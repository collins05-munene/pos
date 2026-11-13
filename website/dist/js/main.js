(()=>{const b=document.querySelector('.burger'),m=document.getElementById('menu');
b&&b.addEventListener('click',()=>{const o=m.classList.toggle('open');b.setAttribute('aria-expanded',o)});
const ul=document.getElementById('items');if(ul){const tot=document.getElementById('total'),ok=document.getElementById('paid');
const I=[["Unga 2kg",190],["Sukari 1kg",160],["Maziwa 500ml",65],["Mkate",60],["Mafuta 1L",320],["Sabuni",85]];
let n=0,s=0;const add=()=>{if(n===I.length){ok.hidden=false;n=-1;return}if(n===-1){ul.innerHTML='';s=0;n=0;ok.hidden=true}
const[x,p]=I[n++];s+=p;const li=document.createElement('li');li.innerHTML='<span>'+x+'</span><span>'+p+'</span>';ul.appendChild(li);tot.textContent='KES '+s.toLocaleString()};
if(matchMedia('(prefers-reduced-motion:reduce)').matches){while(n<I.length)add();add()}else{add();setInterval(add,1300)}}
const f=document.getElementById('cf');if(!f)return;const st=document.getElementById('status');
const R={name:v=>v.trim().length>=2||'Enter your full name.',email:v=>/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)||'Enter a valid email address.',subject:v=>!!v||'Choose a subject.',message:v=>v.trim().length>=10||'Write a message of at least 10 characters.'};
const check=e=>{const r=R[e.name](e.value),er=document.getElementById(e.name+'-e');e.setAttribute('aria-invalid',r!==true);er.textContent=r===true?'':r;return r===true};
f.querySelectorAll('input[name],select,textarea').forEach(e=>R[e.name]&&e.addEventListener('blur',()=>check(e)));
f.addEventListener('submit',async ev=>{ev.preventDefault();st.className='';st.textContent='';
const ok=[...f.elements].filter(e=>R[e.name]).map(check).every(Boolean);if(!ok){st.className='bad';st.textContent='Fix the highlighted fields and send again.';return}
if(f.website.value)return;const a=f.getAttribute('action');if(!a){st.className='bad';st.textContent='The contact form is not connected yet. Please try again later.';return}
try{const r=await fetch(a,{method:'POST',headers:{Accept:'application/json'},body:new FormData(f)});if(!r.ok)throw 0;f.reset();st.className='ok';st.textContent='Message sent. We will reply soon.'}
catch(e){st.className='bad';st.textContent='Your message could not be sent. Check your connection and try again.'}})})();
