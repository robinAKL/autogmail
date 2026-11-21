"""Deterministic preflight explanations. These are checks, not a guarantee."""
import re
from gmail_auto.errors import ConfigError


def insight(item):
    text = item.get('draft','')
    commitments = []
    for line in re.split(r'(?<=[.!?。！？])\s*|\n',text):
        if re.search(r'\b(will|confirm|agree|guarantee|deliver|meet|payment|price|refund)\b|确认|承诺|保证|退款|付款|交付|\d{1,2}:\d{2}|[$€£¥]\s*\d',line,re.I):
            commitments.append(line[:240])
    warnings = []
    if re.search(r'\b(please find attached|see attached|attached (?:is|are)|i(?: have|\'ve)? attached)\b|请查收附件|见附件|已附上|附件中',text,re.I) and not item.get('attachments'):
        warnings.append('The text mentions an attachment, but nothing is attached. Add the file or change the text.')
    if re.search(r'\[\[(?:.*?)\]\]|\{\{(?:.*?)\}\}|\[(?:TODO|待填写|待确认|insert[^\]]*)\]',text,re.I):
        warnings.append('The text still has placeholders to fill in.')
    source_details = []
    if item.get('sources'):
        from gmail_auto import knowledge
        matches = knowledge.search((item.get('subject') or '') + '\n' + text, 6)
        docs = knowledge.list_documents()
        for title in item['sources'][:6]:
            document = next((d for d in docs if d['title']==title),None)
            passage = next((m for m in matches if m['title']==title),None)
            source_details.append({'title':title,'doc_id':document['id'] if document else '', 'text':passage['text'][:650] if passage else ''})
    return {'commitments':commitments[:6], 'warnings':warnings, 'missing':item.get('reason') or 'Check facts, recipients and attachments before sending.', 'sources':item.get('sources') or [], 'source_details':source_details, 'preview_stale':bool(item.get('preview_stale'))}


def validate(item):
    check = insight(item)
    if check['warnings']:
        raise ConfigError('Check before sending: ' + ' '.join(check['warnings']))


def check_latest(item, client):
    if item.get('mode')=='new' or not item.get('thread_id'):
        return
    latest = getattr(client,'latest_thread_timestamp',None)
    if not latest:
        return
    timestamp = latest(item['thread_id'])
    if not isinstance(timestamp, (int, float)):
        return
    if timestamp > float(item.get('reviewed_at') or item.get('created_at') or 0):
        raise ConfigError('New mail arrived in this thread after the draft was written. Open the original email, check it, then click "I have checked the newest mail".')
