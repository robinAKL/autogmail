"""Workspace tests never touch mailbox APIs; DB tests use a disposable owner scope."""
import os
import time
import unittest
import uuid
from datetime import timedelta
from unittest.mock import patch

from gmail_auto import matters, reviews, approvals, knowledge, workspace_db as db
from gmail_auto.approval_checks import insight, validate, check_latest
from gmail_auto.errors import ConfigError
from tests.test_core import TempHome, sample
from tests.test_web import WebCase


class PreflightTests(TempHome):
    def test_missing_attachment_blocks(self):
        with self.assertRaises(ConfigError):
            validate({'draft':'Please see attached.'})
        validate({'draft':'Please see attached.','attachments':[{'id':'kept'}]})

    def test_placeholders_block(self):
        with self.assertRaises(ConfigError):
            validate({'draft':'We will meet at [TODO].'})

    def test_commitments_are_explained(self):
        self.assertTrue(insight({'draft':'I confirm the price of $100.'})['commitments'])

    def test_newer_mail_requires_review(self):
        client = type('Client',(),{'latest_thread_timestamp':lambda _,thread:100})()
        with self.assertRaises(ConfigError):
            check_latest({'thread_id':'t','created_at':90},client)
        check_latest({'thread_id':'t','created_at':90,'reviewed_at':101},client)

    def test_edit_clears_preview_and_sets_stale(self):
        from gmail_auto.models import Draft
        item=approvals.create(sample(),Draft(reply='This is the original reply.',reply_l1='译文'),'en')
        updated=approvals.set_draft(item['id'],'Here is my edited reply.')
        self.assertEqual(updated['draft_l1'],'')
        self.assertTrue(updated['preview_stale'])

    def test_disabled_db_does_not_create_followup(self):
        matters.after_send({'id':'fake','followup_days':3})
        self.assertEqual(matters.summary()['due'],0)

    def test_workday_skips_weekend(self):
        from datetime import datetime,timezone
        with patch('gmail_auto.matters.now',return_value=datetime(2026,11,20,0,0,tzinfo=timezone.utc)):
            day=matters.workdays_after(1)
            self.assertIn('2026-11-23',day)


class WorkspacePageTests(WebCase):
    def test_pages_open_without_db_with_explanation(self):
        for path in ['/matters','/knowledge/review']:
            response=self.client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertIn(b'workspace.css',response.data)

    def test_invalid_create_preserves_input(self):
        with patch.dict(os.environ,{'WORKSPACE_DISABLE_DB':'1'}):
            response=self.client.post('/matters',data={'title':'Keep this title','next_action':'Keep this step'})
        self.assertEqual(response.status_code,400)
        self.assertIn(b'Keep this title',response.data)
        self.assertIn(b'Keep this step',response.data)

    def test_chinese_workspace_translation(self):
        from gmail_auto.settings import save_settings
        save_settings({'ui_language':'zh'})
        text=self.client.get('/matters').get_data(as_text=True)
        self.assertIn('新建事项',text)
        self.assertIn('不会自动发送跟进信',text)


@unittest.skipUnless(os.getenv('DATABASE_URL'),'PostgreSQL not configured')
class WorkspaceDatabaseTests(TempHome):
    def setUp(self):
        super().setUp()
        self.env=patch.dict(os.environ,{'WORKSPACE_DISABLE_DB':'0','WORKSPACE_OWNER':'test-'+uuid.uuid4().hex})
        self.env.start()

    @classmethod
    def setUpClass(cls):
        with patch.dict(os.environ,{'WORKSPACE_DISABLE_DB':'0'}):
            db.migrate()

    def tearDown(self):
        try:
            with db.transaction() as conn:
                conn.execute('DELETE FROM workspace_records WHERE scope=%s',(db.scope(),))
        finally:
            self.env.stop()
            super().tearDown()

    def test_create_complete_reopen_and_version_conflict(self):
        m=matters.create('Course quote','Confirm head count')
        completed=matters.update(m['id'],m['version'],'complete')
        self.assertEqual(completed['status'],'settled')
        with self.assertRaises(ConfigError):
            matters.update(m['id'],m['version'],'save',{'title':'stale'})
        reopened=matters.update(m['id'],completed['version'],'reopen')
        self.assertEqual(reopened['status'],'waiting_on_me')

    def test_duplicate_story_import_is_idempotent(self):
        first=matters.create('A',source_story='same')
        second=matters.create('B',source_story='same')
        self.assertEqual(first['id'],second['id'])

    def test_hidden_is_not_completed(self):
        m=matters.create('A')
        hidden=matters.update(m['id'],m['version'],'hide')
        self.assertEqual(hidden['status'],'waiting_on_me')
        self.assertEqual(matters.listing(),[])
        self.assertEqual(len(matters.listing(True)),1)

    def test_followup_activation_is_idempotent(self):
        item={'id':'approval-x','followup_days':3,'provider':'gmail','account':'me@example.com','thread_id':'t','subject':'Course'}
        matters.after_send(item,'sent1')
        matters.after_send(item,'sent1')
        self.assertEqual(len(matters.listing()),1)
        self.assertEqual(matters.listing()[0]['status'],'waiting_on_them')

    def test_reply_stops_reminder_without_completing(self):
        matters.after_send({'id':'a','followup_days':1,'provider':'gmail','thread_id':'t','account':'me@example.com'})
        mail=sample(id='new',thread_id='t',account='me@example.com',received_ts=time.time()+10)
        matters.note_inbound(mail)
        item=matters.listing()[0]
        self.assertEqual(item['followup_status'],'received')
        self.assertEqual(item['remind_at'],'')
        self.assertEqual(item['status'],'waiting_on_them')
        self.assertTrue(item['new_progress'])

    def test_other_account_and_auto_reply_do_not_stop(self):
        matters.after_send({'id':'a','followup_days':1,'provider':'gmail','thread_id':'t','account':'me@example.com'})
        matters.note_inbound(sample(thread_id='t',account='other@example.com',received_ts=time.time()+10))
        matters.note_inbound(sample(thread_id='t',account='me@example.com',headers={'auto-submitted':'auto-replied'},received_ts=time.time()+10))
        self.assertEqual(matters.listing()[0]['followup_status'],'active')

    def test_snooze_requires_future_and_notifies_once(self):
        m=matters.create('Follow up')
        with self.assertRaises(ConfigError):
            matters.update(m['id'],m['version'],'snooze',{'remind_at':'2020-01-01T12:00'})
        updated=matters.update(m['id'],m['version'],'snooze',{'remind_at':matters.stamp(matters.now()+timedelta(days=1))})
        with patch('gmail_auto.matters.now',return_value=matters.now()+timedelta(days=2)),patch('gmail_auto.whatsapp.ready',return_value=True),patch('gmail_auto.whatsapp.send_text') as send:
            matters.notify_due()
            matters.notify_due()
            self.assertEqual(send.call_count,1)

    def test_shortening_requires_three_distinct_drafts_and_opt_in(self):
        for ident in ['a','a','b']:
            reviews.record_edit(ident,'x'*200,'x'*50)
        self.assertEqual(reviews.preferences(),[])
        reviews.record_edit('c','x'*200,'x'*50)
        self.assertEqual(len(reviews.preferences()),1)
        self.assertEqual(knowledge.load_index()['model']['items'],[])
        reviews.decide('preference:shorter','accept',{})
        self.assertTrue(any(i['kind']=='preference' for i in knowledge.load_index()['model']['items']))
        reviews.decide('preference:shorter','undo',{})
        self.assertFalse(any(knowledge.is_active(i) for i in knowledge.load_index()['model']['items']))

    def test_conflict_review_and_undo_preserve_wording(self):
        from gmail_auto.storage import save_doc
        save_doc('kb_index',{'model':{'items':[{'id':'factA','kind':'fact','text':'Course price per hour is 80 USD.','source':'Old pricing'},{'id':'factB','kind':'fact','text':'Course price per hour is 100 USD.','source':'New pricing'}]}})
        candidate=reviews.candidates()[0]
        reviews.decide(candidate['id'],'keep_b',{})
        items=knowledge.load_index()['model']['items']
        self.assertFalse(knowledge.is_active(next(i for i in items if i['id']=='factA')))
        self.assertEqual(next(i for i in items if i['id']=='factA')['text'],'Course price per hour is 80 USD.')
        reviews.decide(candidate['id'],'undo',{})
        self.assertTrue(all(knowledge.is_active(i) for i in knowledge.load_index()['model']['items']))

    def test_web_create_and_drawer(self):
        from gmail_auto.web import create_app
        app=create_app()
        client=app.test_client()
        response=client.post('/matters',data={'title':'New task','next_action':'Reply tomorrow'})
        self.assertEqual(response.status_code,302)
        response=client.get(response.headers['Location'])
        self.assertEqual(response.status_code,200)
        self.assertIn(b'role="dialog"',response.data)
        self.assertIn(b'Reply tomorrow',response.data)

    def test_send_failure_does_not_activate_reminder(self):
        from gmail_auto.models import Draft
        from tests.test_core import FakeGmail
        item=approvals.create(sample(),Draft(reply='Thanks, I will check the notes.'),'en')
        approvals.configure_review(item['id'],3)
        box=FakeGmail([])
        with patch.object(box,'send_reply',side_effect=ConfigError('Permission denied')):
            with self.assertRaises(ConfigError):
                approvals.approve(item['id'],'web',box)
        self.assertEqual(matters.listing(),[])
        approvals.approve(item['id'],'web',box)
        self.assertEqual(len(matters.listing()),1)

    def test_hosted_kv_claims_and_nested_review_are_persistent(self):
        from gmail_auto.token_store import kv_get,kv_set,claim_key,release_key
        with patch.dict(os.environ,{'WORKSPACE_HOSTED':'1'}):
            kv_set('test-key','hello')
            self.assertEqual(kv_get('test-key'),'hello')
            self.assertTrue(claim_key('test-claim'))
            self.assertFalse(claim_key('test-claim'))
            release_key('test-claim')
            self.assertTrue(claim_key('test-claim'))
            # Knowledge writes share the outer DB transaction, not a nested lock.
            for ident in ['a','b','c']:
                reviews.record_edit(ident,'x'*200,'x'*50)
            reviews.decide('preference:shorter','accept',{})
            index=knowledge.load_index()
            self.assertTrue(index['model']['items'])
            self.assertIn('简短',index['brief']['text'])

    def test_hosted_routes_require_owner_login(self):
        from gmail_auto.web import create_app
        from gmail_auto.accounts import start_verification
        with patch.dict(os.environ,{'WORKSPACE_HOSTED':'1','ALLOWED_EMAILS':''}):
            app=create_app()
            self.assertEqual(app.test_client().get('/matters').status_code,302)
            with self.assertRaises(ConfigError):
                start_verification('stranger@example.com','register')
            self.assertEqual(app.secret_key,create_app().secret_key)

    def test_due_filter_includes_later_today_but_not_completed(self):
        from gmail_auto.web import create_app
        future=matters.now()+timedelta(hours=1)
        m=matters.create('Due today','Check',future.isoformat())
        client=create_app().test_client()
        response=client.get('/matters?show=due')
        self.assertIn(b'Due today',response.data)
        matters.update(m['id'],m['version'],'complete')
        response=client.get('/matters?show=due')
        self.assertNotIn(('open='+m['id']).encode(),response.data)

    def test_whatsapp_explicit_command_updates_same_matter(self):
        m=matters.create('Follow up')
        answer=matters.whatsapp_command('事项完成 '+m['id'][:8])
        self.assertIn('No email was sent',answer)
        self.assertEqual(matters.get(m['id'])['status'],'settled')
        matters.whatsapp_command('matter reopen '+m['id'][:8])
        self.assertNotEqual(matters.get(m['id'])['status'],'settled')


class EnglishSourceTests(unittest.TestCase):
    """Messages are written in English in code; Chinese lives only in the i18n tables."""

    def test_messages_in_code_are_english(self):
        import pathlib
        import re
        message = re.compile(r"(?:ConfigError|flash|warnings\.append|_event|report\.lines\.append|_say)\(\s*f?['\"][^'\"]*[一-鿿]")
        found = []
        for path in pathlib.Path('gmail_auto').glob('*.py'):
            if 'i18n' in path.name:
                continue
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if message.search(line):
                    found.append(f'{path}:{number}')
        self.assertEqual(found, [])

    def test_messages_translate_to_chinese(self):
        from gmail_auto.i18n import text
        with self.assertRaises(ConfigError) as caught:
            validate({'draft':'Please see attached. [TODO]'})
        self.assertEqual(text(str(caught.exception)), '发送前检查：正文提到附件，但还没有附件。请添加文件或修改正文。 正文仍有待填写占位内容。')
        self.assertEqual(text('Matter updated: Lee quote. No email was sent.'), '事项已更新：Lee quote。没有发送邮件。')
        self.assertEqual(text('Reminders checked. Tried to send 2 notifications; every due matter can be handled here.'), '已检查提醒。2 项通知已尝试发送；所有到期事项都可在网页处理。')
        self.assertEqual(text(reviews.CONFLICT), '可能冲突')

    def test_whatsapp_commands_in_both_languages(self):
        for command in ('matters', '事项', 'matter done 0123abcd', '事项完成 0123abcd', 'Matter snooze 0123abcd 2026-12-01 09:00', '事项延后 0123abcd 2026-12-01 09:00'):
            self.assertTrue(matters.is_command(command), command)
        for other in ('matter', 'done 0123abcd', 'reply 3'):
            self.assertFalse(matters.is_command(other), other)
