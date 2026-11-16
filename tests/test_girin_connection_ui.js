const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
let value=true, saved=[], flashes=[], attrs={},label={},button={setAttribute:(k,v)=>attrs[k]=v},navs=[],handlers={};
const c={state:{},Bridge:{live:true,api:{get_game_connection:async()=>({ok:true,auto_connect:value}),set_game_connection:async next=>{saved.push(next);value=next;return {ok:true,auto_connect:value};}}},ic:()=>'',flash:m=>flashes.push(m),setNav:n=>navs.push(n),document:{querySelector:q=>q==='[data-girin-connect]'?button:label,addEventListener:(type,fn)=>handlers[type]=fn}};
vm.createContext(c);vm.runInContext(fs.readFileSync('frontend/nether-glass-launcher-standalone.html','utf8').split('/* Saved game connection setting. */')[1].split('</script>')[0],c);
(async()=>{
 await c.loadGirinConnection();assert.equal(c.state.girinConnection.loaded,true);assert.equal(attrs['aria-checked'],'true');assert.equal(label.textContent,'ON');assert.ok(c.girinConnectionCardHTML().includes('게임 설정'));
 await c.toggleGirinConnection();assert.equal(value,false);assert.equal(attrs['data-on'],'false');assert.equal(label.textContent,'OFF');assert.equal(button.disabled,false);
 await c.toggleGirinConnection();assert.equal(value,true);assert.deepEqual(saved,[false,true]);c.setNav('system');assert.deepEqual(navs,['system']);await new Promise(r=>setImmediate(r));
 c.Bridge.api.set_game_connection=async()=>({ok:false,message:'failed save'});await c.toggleGirinConnection();assert.equal(value,true);assert.equal(c.state.girinConnection.auto_connect,true);assert.equal(flashes.pop(),'failed save');assert.equal(button.disabled,false);
 let resolve;let calls=0;c.Bridge.api.set_game_connection=()=>{calls++;return new Promise(r=>resolve=r);};const pending=c.toggleGirinConnection();assert.equal(button.disabled,true);await c.toggleGirinConnection();assert.equal(calls,1);resolve({ok:true,auto_connect:false});await pending;assert.equal(label.textContent,'OFF');assert.ok(handlers.click);console.log('PASS: connection settings card, ON/OFF save, navigation load, failed-save rollback and busy guard');
})().catch(e=>{console.error(e);process.exitCode=1;});
