"""Small blueprint: workspace UI stays out of the legacy route module."""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from gmail_auto import matters, workspace_db
from gmail_auto.errors import ConfigError


def blueprint():
    bp = Blueprint('workspace', __name__)

    @bp.before_request
    def protect():
        from gmail_auto.web import _login_required
        return _login_required()

    @bp.get('/matters')
    def index():
        error = ''
        items = []
        try:
            items = matters.listing(include_hidden=request.args.get('show')=='hidden')
        except ConfigError as exc:
            error = str(exc)
        show = request.args.get('show','waiting_on_me')
        filtered = [m for m in items if (m.get('hidden') if show=='hidden' else not m.get('hidden') and (show=='all' or (show=='due' and m['status']!='settled' and (m['due_today'] or m.get('remind_at') and m['remind_at'] <= matters.stamp())) or m['status']==show))]
        q = request.args.get('q','').strip().lower()
        if q:
            filtered = [m for m in filtered if q in (m['title'] + ' ' + m.get('next_action','')).lower()]
        filtered.sort(key=lambda m:(not m['overdue'], m.get('due_at') or m.get('remind_at') or '9999', m['title']))
        opened = None
        if request.args.get('open'):
            try:
                opened = matters.get(request.args['open'])
            except ConfigError as exc:
                error = str(exc)
        return render_template('matters.html', items=filtered, opened=opened, states=matters.STATES, show=show, q=q, error=error, zone=str(matters.zone()), seed=request.args, summary=_summary())

    @bp.post('/matters')
    def create():
        try:
            item = matters.create(request.form.get('title',''), request.form.get('next_action',''), request.form.get('due_at',''), request.form.get('mail_key',''), request.form.get('provider','gmail'), request.form.get('account',''), request.form.get('thread_id',''), source_story=request.form.get('source_story',''))
            flash('Matter created. No email will be sent automatically.')
            return redirect(url_for('workspace.index', show='all', open=item['id']))
        except ConfigError as exc:
            flash(str(exc), 'error')
            return render_template('matters.html', items=[], opened=None, states=matters.STATES, show='all', q='', error=str(exc), zone=str(matters.zone()), seed=request.form, summary=_summary()), 400

    @bp.post('/matters/<ident>/<action>')
    def act(ident, action):
        try:
            item = matters.update(ident, request.form.get('version'), action, request.form)
            flash('Saved. Use "Reopen" to undo completion.' if action=='complete' else 'Saved.')
            return redirect(url_for('workspace.index', show='all', open=item['id'], saved='1' if action=='save' else None))
        except ConfigError as exc:
            flash(str(exc), 'error')
            return redirect(url_for('workspace.index', show='all', open=ident))

    @bp.post('/matter-reminders/check')
    def reminders():
        try:
            from gmail_auto.mailboxes import connected_clients
            clients, _ = connected_clients()
            matters.refresh_followups(clients)
            count = matters.notify_due()
            flash(f'Reminders checked. Tried to send {count} notifications; every due matter can be handled here.')
        except ConfigError as exc:
            flash(str(exc),'error')
        return redirect(url_for('workspace.index',show='due'))

    @bp.post('/matters/<ident>/draft-followup')
    def draft(ident):
        try:
            item = matters.get(ident)
            from gmail_auto import writer
            from gmail_auto.profile_store import load_profile
            # Use the original mailbox reply path when possible; never auto-send.
            if item.get('mail_key') and not item['mail_key'].startswith('new:'):
                # Follow-up opens original letter; owner supplies final points there.
                return redirect(url_for('home', open=item['mail_key']))
            from gmail_auto import approvals
            original = approvals.get(item.get('approval_id','')) or {}
            session = writer.start(to=original.get('to') or original.get('from_email',''), subject='Re: ' + item['title'], body='')
            return redirect(url_for('write_page', session_id=session['id']))
        except ConfigError as exc:
            flash(str(exc),'error')
            return redirect(url_for('workspace.index', show='all', open=ident))

    @bp.get('/knowledge/review')
    def review():
        from gmail_auto import reviews
        try:
            return render_template('reviews.html', candidates=reviews.candidates(), preferences=reviews.preferences(), decisions=reviews.history(), zone=str(matters.zone()))
        except ConfigError as exc:
            flash(str(exc), 'error')
            return render_template('reviews.html', candidates=[], preferences=[], decisions=[], zone=str(matters.zone())), 503

    @bp.post('/knowledge/review/<ident>')
    def review_act(ident):
        from gmail_auto import reviews
        try:
            reviews.decide(ident, request.form.get('action',''), request.form)
            flash('Review decision saved. The history is kept.')
        except ConfigError as exc:
            flash(str(exc),'error')
        return redirect(url_for('workspace.review'))

    return bp


def _summary():
    try:
        return matters.summary()
    except ConfigError:
        return {'waiting':0,'due':0,'them':0,'items':[],'available':False}
