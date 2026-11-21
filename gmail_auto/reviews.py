"""Evidence-based knowledge checks and opt-in style suggestions (never auto-learn)."""
import hashlib
import re
from datetime import date
from gmail_auto import workspace_db as store
from gmail_auto.errors import ConfigError

EXPIRED = 'Expiry check'
CONFLICT = 'Possible conflict'


def candidates():
    from gmail_auto import knowledge
    items = knowledge.load_index()['model']['items']
    active = [i for i in items if knowledge.is_active(i)]
    result = []
    for item in items:
        if item.get('valid_until') and item['valid_until'][:10] <= date.today().isoformat() and item.get('status') not in ('ended','superseded'):
            result.append({'id':'expired:' + item['id'], 'kind':EXPIRED,'a':item,'b':None,'reason':'This record is past its valid-until date; replies should no longer use it.'})
    numbered = [i for i in active if re.search(r'\d',i['text']) and i['kind']=='fact']
    for pos, a in enumerate(numbered):
        for b in numbered[pos+1:]:
            ta = set(re.findall(r'[a-z]{3,}|[一-鿿]{2,}',a['text'].lower()))
            tb = set(re.findall(r'[a-z]{3,}|[一-鿿]{2,}',b['text'].lower()))
            na = set(re.findall(r'\d+(?:\.\d+)?',a['text']))
            nb = set(re.findall(r'\d+(?:\.\d+)?',b['text']))
            if ta and tb and len(ta & tb)/max(len(ta | tb),1) >= .5 and na != nb:
                pair = sorted([a['id'],b['id']])
                ident = 'conflict:' + hashlib.sha256('|'.join(pair).encode()).hexdigest()[:20]
                result.append({'id':ident,'kind':CONFLICT,'a':a,'b':b,'reason':'Similar facts contain different numbers. This is only a candidate, not a certain conflict; check the sources.'})
    if not store.available():
        return result[:30]
    with store.transaction() as db:
        decisions = {r['id']:r for r in store.all_records(db,'review')}
    for item in result:
        item['decision'] = decisions.get(item['id'],{})
    return [r for r in result[:30] if r.get('decision',{}).get('action') not in ('keep_a','keep_b','end','dismiss')]


def record_edit(approval_id, before, after):
    if not store.available() or not before or before==after:
        return
    # Collect only a narrow style signal; never turn monetary or date edits into rules.
    if len(after) >= len(before)*.8 or len(before)<80:
        return
    with store.transaction() as db:
        store.put(db,'edit_signal',{'id':str(approval_id),'kind':'shorter','before_length':len(before),'after_length':len(after)})


def preferences():
    if not store.available():
        return []
    with store.transaction() as db:
        edits = store.all_records(db,'edit_signal')
        decision = store.get(db,'review','preference:shorter')
    if len(edits)<3 or (decision or {}).get('action') in ('once','defer','undo'):
        return []
    return [{'id':'preference:shorter','text':'Keep replies short and direct. Keep the needed facts and commitments, and skip empty pleasantries.','count':len(edits),'decision':decision or {}}]


def decide(ident, action, values):
    from gmail_auto import knowledge
    if not store.available():
        raise ConfigError('Knowledge review needs the database to be set up.')
    allowed = {'keep_a','keep_b','end','dismiss','defer','confirm','accept','once','undo'}
    if action not in allowed:
        raise ConfigError('That review action is not supported.')
    suggestion = next(iter(preferences()),None) if ident == 'preference:shorter' else None
    candidate = next((r for r in candidates() if r['id']==ident),None) if ident != 'preference:shorter' and action != 'confirm' else None
    with store.transaction() as db:
        old = store.get(db,'review',ident) or {}
        if ident == 'preference:shorter':
            if action=='accept' and old.get('action')!='accept':
                if not suggestion:
                    raise ConfigError('A suggestion needs shortening edits on at least three different emails.')
                item = knowledge.add_item('preference', suggestion['text'])
                old['knowledge_id'] = item['id']
            elif action=='undo' and old.get('knowledge_id'):
                knowledge.end_item(old['knowledge_id'])
            elif action not in ('once','defer','accept','undo'):
                raise ConfigError('That preference action is not valid.')
        elif action == 'undo':
            if not old.get('before_items'):
                raise ConfigError('This decision has no knowledge state to undo.')
            knowledge.restore_review_items(old['before_items'])
        elif action == 'confirm':
            target = values.get('item_id','')
            item = next((i for i in knowledge.load_index()['model']['items'] if i['id']==target),None)
            if not item:
                raise ConfigError('This knowledge item has changed. Refresh the page.')
            until = values.get('valid_until','')
            if until:
                try:
                    date.fromisoformat(until)
                except ValueError:
                    raise ConfigError('Enter the valid-until date as YYYY-MM-DD.')
            old['before_items'] = [item]
            knowledge.confirm_item(target, until)
            old.update(item_id=target,valid_until=until,confirmed_at=date.today().isoformat(), source=item.get('source',''))
        else:
            if not candidate:
                raise ConfigError('This candidate has changed or was already reviewed. Refresh the page.')
            old['before_items'] = [candidate['a']] + ([candidate['b']] if candidate['b'] else [])
            old['label'] = candidate['a']['text'][:100]
            if action=='keep_a' and candidate['b']:
                knowledge.end_item(candidate['b']['id'])
            elif action=='keep_b' and candidate['b']:
                knowledge.end_item(candidate['a']['id'])
            elif action=='end':
                knowledge.end_item(candidate['a']['id'])
        history = old.get('history',[])
        from gmail_auto.matters import stamp
        history.append({'at':stamp(),'action':action})
        store.put(db,'review',{**old,'id':ident,'action':action,'history':history[-50:]})


def history():
    if not store.available():
        return []
    with store.transaction() as db:
        return store.all_records(db,'review')[:30]


def unresolved_conflicts():
    return [r for r in candidates() if r['kind']==CONFLICT and r.get('decision',{}).get('action') != 'dismiss']
