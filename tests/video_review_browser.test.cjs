// Exercise the shipped browser script with an event-capable DOM/video stand-in.
// Run: node --test tests/video_review_browser.test.cjs
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const template=fs.readFileSync(path.join(__dirname,'../infra/video_review.html'),'utf8');
function setup(){
 const elements=new Map(), data={schema_version:1,capture_key:'test/capture',raw_sha256:'abc',segment:0,algorithm:{signal:'Magnitude'},low:0,high:20,
 spans:[[[0,1,1,0],[2,3,2,1],[3,1,1,0]],[[7,1,1,0],[8,3,2,1],[20,1,1,0]]]};
 const context=new Proxy({},{get:()=>()=>{}});
 function element(){return {value:'0',checked:false,textContent:'',files:[],children:[],clientWidth:650,currentTime:0,duration:30,paused:true,
   getContext:()=>context,addEventListener(name,fn){this[name]=fn;},append(...nodes){this.children.push(...nodes);},
   replaceChildren(){this.children=[];},getBoundingClientRect:()=>({left:0,width:650}),pause(){this.paused=true;},
   play(){this.paused=false;return Promise.resolve();},load(){},click(){},};}
 for(const m of template.matchAll(/id="([^"]+)"/g))elements.set(m[1],element());
 elements.get('review-data').textContent=JSON.stringify(data);elements.get('window').value='10';elements.get('step').value='0.033333';elements.get('kind').value='疑似漏计';elements.get('note').value='';
 const sandbox={document:{getElementById:id=>elements.get(id),createElement:element},window:{devicePixelRatio:1,addEventListener(){}},URL:{createObjectURL:()=> 'blob:test',revokeObjectURL(){}},Blob,console,setTimeout:()=>{},requestAnimationFrame:()=>{}};
 vm.createContext(sandbox);vm.runInContext(template.match(/<script>([\s\S]*?)<\/script>/)[1],sandbox);
 return {elements,run:code=>vm.runInContext(code,sandbox)};
}
test('single-point offset, inverse seek, native-video cursor and slow-play controls',()=>{
 const {elements:e,run}=setup();run('applyMapping(makeMapping(8.4,3.1))');
 assert.ok(Math.abs(run('deviceTime(20)')-14.7)<1e-9);
 assert.ok(Math.abs(run('videoTime(14.7)')-20)<1e-9);
 e.get('video').currentTime=20;e.get('video').timeupdate();assert.match(e.get('clock').textContent,/14\.700/);
 e.get('speed').value='0.25';e.get('speed').onchange();assert.equal(e.get('video').playbackRate,.25);
 e.get('forward').onclick();assert.ok(Math.abs(e.get('video').currentTime-20.033333)<1e-9);assert.equal(e.get('video').paused,true);
 // Clicking the center seeks to the plotted device cursor, using the inverse mapping.
 e.get('wave').onclick({clientX:342.5});assert.ok(Math.abs(e.get('video').currentTime-20.033333)<1e-6);
});
test('two-point correction and rejection of reversed, outside, or empty sync points',()=>{
 const {elements:e,run}=setup();run('applyMapping(makeMapping(1,2,11,13))');
 assert.ok(Math.abs(run('deviceTime(6)')-7.5)<1e-9);assert.ok(Math.abs(run('videoTime(13)')-11)<1e-9);
 assert.throws(()=>run('makeMapping(1,2,1,13)'));
 assert.throws(()=>run('makeMapping(1,2,10,1)'));
 assert.throws(()=>run('makeMapping(31,2)'));
 assert.throws(()=>run('makeMapping(1,25)'));
 e.get('v1').value='';e.get('align').onclick();assert.match(e.get('status').textContent,/完整/);
});
test('gaps remain visible, peaks can be located, and markers follow revised alignment',()=>{
 const {elements:e,run}=setup();run('applyMapping(makeMapping(0,0));videoMeta={name:"test.webm",size:1,lastModified:1}');
 e.get('video').currentTime=4;run('draw()');assert.match(e.get('status').textContent,/缺口/);
 e.get('next').onclick();assert.equal(e.get('video').currentTime,8);
 e.get('prev').onclick();assert.equal(e.get('video').currentTime,2);
 e.get('mark').onclick();assert.equal(run('marks.length'),1);assert.equal(run('record().marks[0].time'),2);
 run('applyMapping(makeMapping(0,1))');assert.equal(run('record().marks[0].time'),3);
 assert.equal(e.get('marks').children.length,1);
});
test('restored records reject another capture and a changed video clears old alignment',async()=>{
 const {elements:e,run}=setup();
 e.get('restore').files=[{text:async()=>JSON.stringify({schema_version:1,capture_key:'other'})}];
 await e.get('restore').onchange();assert.match(e.get('status').textContent,/不一致/);
 run('applyMapping(makeMapping(0,0));videoMeta={name:"old",size:1,lastModified:1};marks=[{video_time:2,kind:"真实动作",note:""}]');
 e.get('file').files=[{name:'new',size:2,lastModified:2}];e.get('file').onchange();
 assert.equal(run('mapping'),null);assert.equal(run('marks.length'),0);
});
