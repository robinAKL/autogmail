(function(){
  // Native navigation preserves server-rendered form behavior; dialogs are keyboard accessible.
  function openNewMatter(){var details=document.getElementById('new-matter');if(details&&location.hash==='#new-matter'){details.open=true;var input=details.querySelector('input[name="title"]');if(input)input.focus();}}
  window.addEventListener('hashchange',openNewMatter);openNewMatter();
  var dialog=document.querySelector('.workspace-drawer');
  if(dialog){
    dialog.focus({preventScroll:true});
    dialog.scrollTop=0;
    document.addEventListener('keydown',function(e){
      if(e.key==='Escape'){var close=dialog.querySelector('[data-dialog-close]');if(close)location.href=close.href;}
      if(e.key==='Tab'){
        var els=Array.from(dialog.querySelectorAll('a[href],button,input,select,textarea,[tabindex="0"]')).filter(function(el){return !el.disabled&&el.getClientRects().length;});
        if(!els.length)return;
        if(e.shiftKey&&(document.activeElement===els[0]||document.activeElement===dialog)){e.preventDefault();els[els.length-1].focus();}
        else if(!e.shiftKey&&document.activeElement===els[els.length-1]){e.preventDefault();els[0].focus();}
      }
    });
  }
  document.querySelectorAll('textarea[name="draft"]').forEach(function(input){
    var original=input.value, notice;
    input.addEventListener('input',function(){
      if(input.value===original)return;
      var box=input.closest('form');
      box.querySelectorAll('.seamless-bilingual').forEach(function(el){el.hidden=true;});
      if(!notice){notice=document.createElement('p');notice.className='preview-dirty';notice.setAttribute('role','status');notice.textContent=document.documentElement.lang.indexOf('zh')===0?'正文已修改，旧母语预览不再有效。请以实际发送内容为准。':'The text changed. The old first-language preview is no longer valid. Check the actual text to send.';input.before(notice);}
    });
  });
  // Keep explicit local recovery copies for matter edits only, never email bodies/tokens.
  document.querySelectorAll('.workspace-drawer form[action$="/save"]').forEach(function(form){
    var key='matter-recovery:'+form.action;
    try{
      if(new URLSearchParams(location.search).get('saved')==='1')sessionStorage.removeItem(key);
      var saved=JSON.parse(sessionStorage.getItem(key)||'null');
      if(saved){var banner=document.createElement('p');banner.className='banner warn';banner.textContent=document.documentElement.lang.indexOf('zh')===0?'有上次未确认保存的事项输入。':'There is a local recovery copy of your last edit.';var restore=document.createElement('button');restore.type='button';restore.className='quiet small';restore.textContent=document.documentElement.lang.indexOf('zh')===0?'恢复输入':'Restore input';restore.onclick=function(){['title','next_action','due_at'].forEach(function(k){if(form.elements[k]&&saved[k]!==undefined)form.elements[k].value=saved[k];});banner.remove();};banner.appendChild(restore);form.prepend(banner);}
      form.addEventListener('submit',function(){var data={};['title','next_action','due_at'].forEach(function(k){data[k]=form.elements[k].value;});sessionStorage.setItem(key,JSON.stringify(data));});
    }catch(e){}
  });
})();
