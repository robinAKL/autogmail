"""Persistent matter state and non-sending follow-up reminders."""
import hashlib
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from gmail_auto import workspace_db as store
from gmail_auto.errors import ConfigError

STATES = {'waiting_on_me':'Waiting on you', 'waiting_on_them':'Waiting on them', 'in_progress':'In progress', 'settled':'Completed'}


def zone():
    try:
        return ZoneInfo(os.getenv('WORKSPACE_TIMEZONE', 'Pacific/Auckland'))
    except Exception:
        return ZoneInfo('UTC')


def now():
    return datetime.now(timezone.utc)


def stamp(value=None):
    return (value or now()).isoformat()


def parse_local(value):
    if not value:
        return ''
    try:
        date = datetime.fromisoformat(value)
        if not date.tzinfo:
            naive = date
            date = date.replace(tzinfo=zone())
            if date.astimezone(timezone.utc).astimezone(zone()).replace(tzinfo=None) != naive:
                raise ValueError('This local time does not exist due to daylight saving time.')
        return date.astimezone(timezone.utc).isoformat()
    except ValueError:
        raise ConfigError('Enter a valid date and time.')


def display_time(value):
    if not value:
        return 'Not set'
    try:
        return datetime.fromisoformat(value).astimezone(zone()).strftime('%Y-%m-%d %H:%M')
    except ValueError:
        return value


def _decorate(item):
    item = dict(item)
    item['label'] = STATES.get(item['status'], item['status'])
    item['due_text'] = display_time(item.get('due_at'))
    item['due_local'] = datetime.fromisoformat(item['due_at']).astimezone(zone()).strftime('%Y-%m-%dT%H:%M') if item.get('due_at') else ''
    item['overdue'] = bool(item.get('due_at') and item['due_at'] <= stamp() and item['status'] != 'settled')
    item['due_today'] = bool(item.get('due_at') and datetime.fromisoformat(item['due_at']).astimezone(zone()).date() <= now().astimezone(zone()).date() and item['status'] != 'settled')
    item['remind_text'] = display_time(item.get('remind_at'))
    return item


def _event(item, text):
    item.setdefault('events', []).append({'at':stamp(), 'text':text})
    item['events'] = item['events'][-100:]
    item['updated_at'] = stamp()


def create(title, next_action='', due_at='', mail_key='', provider='gmail', account='', thread_id='', source='manual', source_story=''):
    title = ' '.join((title or '').split())[:200]
    if not title:
        raise ConfigError('Enter a name for the matter.')
    with store.transaction() as db:
        # A repeated import of the same storyline must not create duplicates.
        if source_story:
            old = next((m for m in store.all_records(db, 'matter') if m.get('source_story') == source_story), None)
            if old:
                return _decorate(old)
        item = {'id':uuid.uuid4().hex, 'title':title, 'status':'waiting_on_me', 'next_action':next_action[:1000], 'due_at':parse_local(due_at), 'remind_at':'', 'hidden':False, 'provider':provider or 'gmail', 'account':account, 'thread_id':thread_id, 'mail_key':mail_key, 'source':source, 'source_story':source_story, 'created_at':stamp(), 'events':[], 'new_progress':False}
        _event(item, 'Matter created')
        return _decorate(store.put(db, 'matter', item))


def listing(include_hidden=False):
    with store.transaction() as db:
        items = store.all_records(db, 'matter')
    return [_decorate(m) for m in items if include_hidden or not m.get('hidden')]


def get(ident):
    with store.transaction() as db:
        m = store.get(db, 'matter', ident)
    if not m:
        raise ConfigError('This matter was not found.')
    return _decorate(m)


def update(ident, version, action, values=None):
    values = values or {}
    with store.transaction() as db:
        item = store.get(db, 'matter', ident)
        if not item:
            raise ConfigError('This matter was not found.')
        if str(item['version']) != str(version):
            raise ConfigError('This matter was changed on another page. Refresh and try again; your changes were not saved.')
        if action == 'save':
            status = values.get('status', item['status'])
            if status not in STATES:
                raise ConfigError('That matter status is not valid.')
            title = ' '.join(values.get('title', item['title']).split())[:200]
            if not title:
                raise ConfigError('The matter name cannot be empty.')
            item.update(title=title, status=status, next_action=values.get('next_action','')[:1000], due_at=parse_local(values.get('due_at','')), new_progress=False)
            if values.get('remind_at'):
                item['remind_at'] = parse_local(values['remind_at'])
                item['reminder_notified'] = False
        elif action == 'complete':
            item['previous_status'] = item['status']
            item['status'] = 'settled'
            item['completed_at'] = stamp()
        elif action == 'reopen':
            item['status'] = item.get('previous_status') or 'waiting_on_me'
            item['new_progress'] = False
            item['hidden'] = False
        elif action == 'snooze':
            item['remind_at'] = parse_local(values.get('remind_at'))
            if not item['remind_at'] or item['remind_at'] <= stamp():
                raise ConfigError('Choose a reminder time in the future.')
            item['reminder_notified'] = False
        elif action == 'hide':
            item['hidden'] = True
        elif action == 'restore':
            item['hidden'] = False
        elif action == 'close_reminder':
            item['remind_at'] = ''
            item['followup_status'] = 'closed'
        else:
            raise ConfigError('That action is not supported.')
        _event(item, {'save':'Matter updated', 'complete':'Marked complete (can be reopened)','reopen':'Reopened','snooze':'Reminder snoozed','hide':'Hidden from the list','restore':'Restored to the list','close_reminder':'Follow-up reminder closed'}.get(action,action))
        return _decorate(store.put(db, 'matter', item))


def summary():
    if not store.available():
        return {'waiting':0, 'due':0, 'them':0, 'items':[], 'available':False}
    items = listing()
    today = now().astimezone(zone()).date()
    due = [m for m in items if m['status'] != 'settled' and ((m.get('due_at') and datetime.fromisoformat(m['due_at']).astimezone(zone()).date() <= today) or (m.get('remind_at') and m['remind_at'] <= stamp()))]
    return {'waiting':sum(m['status']=='waiting_on_me' for m in items), 'due':len(due), 'them':sum(m['status']=='waiting_on_them' for m in items), 'items':due[:8], 'available':True}


def workdays_after(days):
    value = now().astimezone(zone())
    remaining = days
    while remaining:
        value += timedelta(days=1)
        if value.weekday() < 5:
            remaining -= 1
    return stamp(value.astimezone(timezone.utc))


def after_send(item, outbound_id=''):
    """Idempotent activation. Called only after the provider accepted the send."""
    if not store.available():
        return
    days = int(item.get('followup_days') or 0)
    if days < 1:
        return
    ident = hashlib.sha256(('approval:' + item['id']).encode()).hexdigest()[:32]
    with store.transaction() as db:
        if store.get(db, 'matter', ident):
            return
        matter = {'id':ident, 'title':item.get('subject') or 'Email follow-up', 'status':'waiting_on_them', 'next_action':'If they have not replied, check whether to follow up', 'due_at':'', 'remind_at':workdays_after(days), 'provider':item.get('provider','gmail'), 'account':item.get('account',''), 'thread_id':item.get('thread_id',''), 'mail_key':item.get('key',''), 'approval_id':item['id'], 'outbound_message_id':outbound_id, 'followup_status':'active', 'hidden':False, 'created_at':stamp(), 'events':[], 'sent_at':stamp(), 'reminder_notified':False, 'manual_matching':not bool(item.get('thread_id'))}
        _event(matter, 'Email sent; follow-up reminder turned on')
        store.put(db, 'matter', matter)


def note_inbound(mail):
    if not store.available():
        return
    from gmail_auto.filters import is_system_address
    headers = {k.lower():v for k,v in (mail.headers or {}).items()}
    if is_system_address(mail.from_email) or headers.get('auto-submitted','no').lower() != 'no' or 'out of office' in (mail.subject or '').lower():
        return
    if not mail.thread_id:
        return
    if mail.account and mail.from_email.lower() == mail.account.lower():
        return  # Sent messages returned by a thread query are not inbound replies.
    with store.transaction() as db:
        for item in store.all_records(db, 'matter'):
            if item.get('provider') != mail.provider or item.get('thread_id') != mail.thread_id:
                continue
            if item.get('account') and mail.account and item['account'].lower() != mail.account.lower():
                continue
            if item.get('last_inbound_id') == mail.id:
                continue
            if item.get('sent_at') and (not mail.received_ts or mail.received_ts <= datetime.fromisoformat(item['sent_at']).timestamp()):
                continue
            item['last_inbound_id'] = mail.id
            item['new_progress'] = True
            if item.get('followup_status') == 'active':
                item['followup_status'] = 'received'
                item['remind_at'] = ''
            _event(item, 'New activity arrived. Check progress; the matter was not completed automatically.')
            store.put(db, 'matter', item)


def tick():
    if not store.available():
        return
    from gmail_auto.mailboxes import connected_clients
    clients, _ = connected_clients()
    refresh_followups(clients)
    notify_due()


def refresh_followups(clients):
    if not store.available():
        return
    items = [m for m in listing() if m.get('followup_status')=='active' and m.get('thread_id') and m['status']!='settled']
    checked = set()
    for client in clients:
        listing_thread = getattr(client,'list_thread',None)
        if not listing_thread:
            continue
        account = client.get_user_email().lower()
        for item in items:
            key = (item['provider'],item.get('account','').lower(),item['thread_id'])
            if key in checked or item['provider'] != client.provider or key[1] and key[1] != account:
                continue
            checked.add(key)
            try:
                for mail in listing_thread(item['thread_id']):
                    mail.account = account
                    note_inbound(mail)
            except ConfigError:
                # Suppress an automated reminder until its thread can be checked.
                with store.transaction() as db:
                    current = store.get(db,'matter',item['id'])
                    if current:
                        current['thread_check_failed'] = True
                        store.put(db,'matter',current)
                continue
            with store.transaction() as db:
                current = store.get(db,'matter',item['id'])
                if current and current.get('thread_check_failed'):
                    current['thread_check_failed'] = False
                    store.put(db,'matter',current)


def notify_due():
    """One claim per reminder version. Unknown send results are not auto-retried."""
    if not store.available():
        return 0
    from gmail_auto import whatsapp
    if not whatsapp.ready():
        return 0
    pending = []
    with store.transaction() as db:
        for item in store.all_records(db, 'matter'):
            if item.get('hidden') or item.get('status')=='settled' or item.get('reminder_notified') or item.get('thread_check_failed'):
                continue
            if not item.get('remind_at') or item['remind_at'] > stamp():
                continue
            item['reminder_notified'] = True
            item['notification_status'] = 'sending'
            store.put(db, 'matter', item)
            pending.append(item)
    for item in pending:
        try:
            origin = os.getenv('APP_PUBLIC_URL','').rstrip('/')
            link = (origin + '/matters?show=all&open=' + item['id']) if origin.startswith('https://') else _say('See Matters on the website.')
            lines = ['📌 ' + _say('Follow-up reminder: ' + item['title']), item.get('next_action',''), link, _say('No follow-up email is sent automatically.')]
            whatsapp.send_text('\n'.join(line for line in lines if line))
            status = 'sent'
        except Exception:
            status = 'unknown'
        with store.transaction() as db:
            current = store.get(db, 'matter', item['id'])
            if current and current.get('remind_at') == item['remind_at']:
                current['notification_status'] = status
                _event(current, 'Follow-up notice sent' if status=='sent' else 'Notice delivery is uncertain. Check the Matters page; it will not be sent again automatically.')
                store.put(db, 'matter', current)
    return len(pending)


_COMMAND_WORDS = {'完成':'complete', 'done':'complete', '重开':'reopen', 'reopen':'reopen', '延后':'snooze', 'snooze':'snooze'}
_COMMAND = re.compile(r'(?i)(?:事项|matter\s+)(完成|重开|延后|done|reopen|snooze)\s+([a-f0-9]{8,32})(?:\s+(.+))?')


def _say(text):
    """WhatsApp messages follow the interface language; there is no page to translate them on the way out."""
    try:
        from gmail_auto.i18n import text as translate
        from gmail_auto.settings import load_settings
        return translate(text, load_settings().get('ui_language') or 'en')
    except Exception:
        return text


def is_command(text):
    text = (text or '').strip()
    return text.lower() in ('matters', '事项', '待办') or bool(_COMMAND.fullmatch(text))


def whatsapp_command(text):
    """Explicit commands avoid confusing a matter id with an approval number."""
    if text.strip().lower() in ('matters','事项','待办'):
        items = [m for m in listing() if m['status']!='settled'][:10]
        if not items:
            return _say('No open matters.')
        return '\n'.join([_say('Matters (synced with the website)')] + [m['id'][:8] + ' · ' + m['title'] + ' · ' + _say(m['label']) for m in items] + [_say('Commands: matter done ID / matter reopen ID / matter snooze ID YYYY-MM-DD HH:MM')])
    match = _COMMAND.fullmatch(text.strip())
    if not match:
        return None
    action, prefix, value = match.groups()
    found = [m for m in listing() if m['id'].startswith(prefix)]
    if len(found)!=1:
        raise ConfigError(_say('Matter ID not found or not unique. Send "matters" to see the list.'))
    item = found[0]
    update(item['id'], item['version'], _COMMAND_WORDS[action.lower()], {'remind_at':value or ''})
    return _say('Matter updated: ' + item['title'] + '. No email was sent.')


def digest_block():
    data = summary()
    if not data['items']:
        return ''
    return '\n'.join(['📌 ' + _say('Due matters')] + ['- ' + m['title'] + ': ' + (m.get('next_action') or _say(m['label'])) for m in data['items']])
