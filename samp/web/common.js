const $=id=>document.getElementById(id);const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,options={}){const response=await fetch(url,options);let body={};try{body=await response.json()}catch{}if(!response.ok)throw new Error(body.detail||`请求失败（${response.status}）`);return body}
function toast(message,error=false){const node=$('toast');node.textContent=message;node.style.background=error?'#a3271a':'#17345f';node.classList.add('show');setTimeout(()=>node.classList.remove('show'),3200)}
function localDate(value){const date=new Date(value*1000),pad=n=>String(n).padStart(2,'0');return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`}
function timestamp(value){return Math.floor(new Date(value).getTime()/1000)}
function checkedFields(container){return [...container.querySelectorAll('input[type=checkbox]:checked')].map(input=>input.value)}
function renderFieldChecks(container,fields,checked){fields=fields.filter(field=>field!=='row_key');container.innerHTML=fields.length?fields.map(field=>`<label class='field-check'><input type='checkbox' value='${esc(field)}' ${checked.includes(field)?'checked':''}>${esc(field)}</label>`).join(''):'<span class="muted">没有可用字段</span>'}
function setAllFields(container,checked){container.querySelectorAll('input[type=checkbox]').forEach(input=>{input.checked=checked});container.dispatchEvent(new Event('change'))}
