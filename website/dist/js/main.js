(()=>{const b=document.querySelector('.burger'),m=document.getElementById('menu');
b&&b.addEventListener('click',()=>{const o=m.classList.toggle('open');b.setAttribute('aria-expanded',o)});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&m.classList.contains('open')){m.classList.remove('open');b.setAttribute('aria-expanded',false);b.focus()}});
const f=document.getElementById('cf');if(!f)return;const st=document.getElementById('status');
const rules={name:v=>v.trim().length>=2||'Enter your full name.',email:v=>/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)||'Enter a valid email address.',subject:v=>!!v||'Choose a subject.',message:v=>v.trim().length>=10||'Write a message of at least 10 characters.'};
const chk=k=>{const el=f.elements[k],r=rules[k](el.value),e=document.getElementById('e-'+k);e.textContent=r===true?'':r;el.setAttribute('aria-invalid',r!==true);return r===true};
Object.keys(rules).forEach(k=>f.elements[k].addEventListener('blur',()=>chk(k)));
f.addEventListener('submit',async e=>{e.preventDefault();st.className='';st.textContent='';
const ok=Object.keys(rules).map(chk).every(Boolean);if(!ok){st.className='bad';st.textContent='Please fix the highlighted fields.';return}
if(f.elements._gotcha.value)return;
if(!f.getAttribute('action')){st.className='bad';st.textContent='The contact form is not connected yet. Please try again later.';return}
const btn=f.querySelector('button');btn.disabled=true;
try{const r=await fetch(f.action,{method:'POST',body:new FormData(f),headers:{Accept:'application/json'}});
if(!r.ok)throw 0;f.reset();st.className='ok';st.textContent='Thanks. Your message has been sent and we will get back to you.'}
catch{st.className='bad';st.textContent='Your message could not be sent. Please try again.'}btn.disabled=false})})();
