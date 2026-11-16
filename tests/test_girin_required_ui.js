const vm=require('node:vm'), fs=require('node:fs'), assert=require('node:assert/strict');
let calls=0, rendered=0, timer, events={}, originalLaunches=0;
let response={ok:true,changed:true,statuses:{'giraffe-clientinfo':'installed'},versions:{'giraffe-clientinfo':{installed:'1.19.14',latest:'1.19.14'}}};
const mod=()=>({id:'giraffe-clientinfo',title:'Giraffe ClientInfo',required:true,state:'install',_installedVersion:null});
const c={console,Set,Bridge:{live:true,api:{refresh_required_mods:async()=>{calls++;return response;}}},DATA:{mods:[mod(),{id:'other',title:'Other',state:'installed'}]},state:{booting:false,curatedMods:[mod()],installs:{'Giraffe ClientInfo':'install','Other':'busy'}},refreshItemInPlace:()=>rendered++,renderSidebar:()=>{},refreshModalAction:()=>{},doLaunch:async()=>{originalLaunches++;return 'launched';},window:{addEventListener:(name,fn)=>events[name]=fn},document:{hidden:false,addEventListener:(name,fn)=>events[name]=fn},setInterval:(fn,ms)=>{assert.equal(ms,2000);timer=fn;return 1;}};
vm.createContext(c);vm.runInContext(fs.readFileSync('frontend/nether-glass-launcher-standalone.html','utf8').split('/* Private test: refresh required cards from local files, without a full network scan. */')[1].split('/* Saved game connection setting. */')[0],c);
(async()=>{
 await c.refreshGirinRequired(true);assert.equal(c.DATA.mods[0].state,'installed');assert.equal(c.state.curatedMods[0]._installedVersion,'1.19.14');assert.equal(c.state.installs.Other,'busy');assert.equal(c.DATA.mods[1].state,'installed');assert.equal(rendered,1);
 await c.refreshGirinRequired(true);assert.equal(rendered,1);
 c.DATA.mods[0]._busy=true;let before=calls;await c.refreshGirinRequired();assert.equal(calls,before);c.DATA.mods[0]._busy=false;
 response={ok:true,changed:true,statuses:{'giraffe-clientinfo':'not_installed'},versions:{}};await events.focus();assert.equal(c.DATA.mods[0].state,'install');assert.equal(c.DATA.mods[0]._installedVersion,null);
 response={ok:true,changed:true,statuses:{'giraffe-clientinfo':'installed'},versions:{'giraffe-clientinfo':{installed:'1.19.15'}}};assert.equal(await c.doLaunch(),'launched');assert.equal(originalLaunches,1);assert.equal(c.DATA.mods[0]._installedVersion,'1.19.15');assert.ok(timer);assert.ok(events.visibilitychange);console.log('PASS: required UI refresh, cached copies, unrelated install state, busy guard, focus, launch and timer');
})().catch(e=>{console.error(e);process.exitCode=1;});
