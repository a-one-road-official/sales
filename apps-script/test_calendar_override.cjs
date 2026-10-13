const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(__dirname + '/calendar_override.gs', 'utf8')
  .replaceAll('16ec099z@gmail.com', 'source@example.test')
  .replaceAll('admin@a1-road.com', 'dest@example.test');
// Synthetic data only: no customer or private calendar identifiers in tests.
const t = h => `2020-01-07T${h}:00:00+09:00`;
const rule = {source_calendar:'source@example.test',action:'exclude_all_day_instance',source_event_id:'annotation_20200107',start_date:'2020-01-07',end_date:'2020-01-08',timezone:'Asia/Tokyo'};
const host = {organizer:{email:'dest@example.test'},attendees:[],description:'A1_MIRROR_OVERRIDE_V1='+JSON.stringify(rule)};
const annotation = {id:rule.source_event_id,start:{date:rule.start_date},end:{date:rule.end_date}};
const timed = (id,start,end) => ({id,start:{dateTime:start},end:{dateTime:end}});
const baseEvents = [annotation,timed('sleep',t('00'),t('06')),timed('routine',t('08'),'2020-01-07T08:30:00+09:00')];
const request = {timeMin:t('00'),timeMax:'2020-01-09T00:00:00+09:00',items:[{id:rule.source_calendar}]};
const initialBusy = [{start:t('00'),end:'2020-01-08T07:00:00+09:00'}];
function run({events=baseEvents,hosts=[host],busy=initialBusy,tz='Asia/Tokyo',req=request}={}) {
 const clone=x=>JSON.parse(JSON.stringify(x));
 const context={CONFIG:{SOURCE_CALENDAR_ID:rule.source_calendar,DESTINATION_CALENDAR_ID:'dest@example.test'},console:{log(){}},Calendar:{Freebusy:{query:()=>({calendars:{[rule.source_calendar]:{busy:clone(busy)}}})},Events:{list:(id)=>({items:clone(id===rule.source_calendar?events:hosts),timeZone:tz})}}};
 vm.createContext(context);vm.runInContext(code,context);
 return clone(context.a1MirrorFreebusyQueryV6_(req).calendars[rule.source_calendar].busy);
}
const iso=s=>new Date(s).toISOString();
let n=0;const check=(name,fn)=>{fn();n++;console.log('PASS',name)};
check('excluded annotation removed; timed events and next-day tail retained',()=>assert.deepEqual(run(),[
 {start:iso(t('00')),end:iso(t('06'))},{start:iso(t('08')),end:iso('2020-01-07T08:30:00+09:00')},
 {start:iso('2020-01-08T00:00:00+09:00'),end:iso('2020-01-08T07:00:00+09:00')} ]));
check('no rule leaves FreeBusy unchanged',()=>assert.deepEqual(run({hosts:[]}),initialBusy));
check('actual meeting inside removed annotation remains busy',()=>assert(run({events:[...baseEvents,timed('meeting',t('15'),t('16'))]}).some(b=>b.start===iso(t('15'))&&b.end===iso(t('16')))));
check('another genuine all-day commitment is retained',()=>assert.deepEqual(run({events:[...baseEvents,{...annotation,id:'different'}]}),initialBusy.map(b=>({start:iso(b.start),end:iso(b.end)}))));
check('external organizer cannot create an override',()=>assert.deepEqual(run({hosts:[{...host,organizer:{email:'external@example.test'}}]}),initialBusy));
check('shared event cannot carry override',()=>assert.deepEqual(run({hosts:[{...host,attendees:[{email:'external@example.test'}]}]}),initialBusy));
check('source completeness mismatch aborts',()=>assert.throws(()=>run({events:baseEvents.slice(1)}),/MISMATCH/));
check('unverified all-day timezone aborts',()=>assert.throws(()=>run({tz:'UTC'}),/TIMEZONE/));
check('changed event ID is not suppressed',()=>assert.deepEqual(run({events:[{...annotation,id:'moved'},...baseEvents.slice(1)]}),initialBusy.map(b=>({start:iso(b.start),end:iso(b.end)}))));
check('a source instance changed to timed is not suppressed',()=>assert.deepEqual(run({events:[timed(rule.source_event_id,t('00'),'2020-01-08T00:00:00+09:00'),...baseEvents.slice(1)]}),initialBusy.map(b=>({start:iso(b.start),end:iso(b.end)}))));
console.log(`TESTS_OK=${n}`);
