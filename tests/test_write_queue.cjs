'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {LatestWriteQueue} = require('../write_queue.js');
const gate = () => {let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};

test('a slow first write retains the last drag value without an unbounded backlog', async () => {
  const queue=new LatestWriteQueue(0), first=gate(), sent=[];
  const jobs=[queue.enqueue('gain',async()=>{sent.push(0);await first.promise;return 0;})];
  for(let value=1;value<=100;value++) jobs.push(queue.enqueue('gain',async()=>{sent.push(value);return value;}));
  assert.equal(queue.pending.size,1);
  first.resolve();const results=await Promise.all(jobs);await queue.idle();
  assert.deepEqual(sent,[0,100]);assert.equal(results[0],0);assert.ok(results.slice(1).every(value=>value===100));
});

test('independent channels and fields are retained and requests never overlap', async () => {
  const queue=new LatestWriteQueue(0), first=gate(), sent=[];let concurrent=0,max=0;
  const run=(name,wait)=>async()=>{max=Math.max(max,++concurrent);sent.push(name);if(wait)await wait;--concurrent;};
  const jobs=[queue.enqueue('input1',run('first',first.promise)),queue.enqueue('input1',run('gain')),
    queue.enqueue('input2',run('other channel')),queue.enqueue('pan',run('pan'))];
  first.resolve();await Promise.all(jobs);assert.equal(max,1);assert.deepEqual(sent,['first','gain','other channel','pan']);
});

test('replaced updates keep the order of the newest user action across link and fader edits', async () => {
  const queue=new LatestWriteQueue(0), first=gate(), sent=[];
  const jobs=[queue.enqueue('gain',async()=>first.promise),queue.enqueue('gain',async()=>sent.push('old gain')),
    queue.enqueue('link',async()=>sent.push('link')),queue.enqueue('gain',async()=>sent.push('new gain'))];
  first.resolve();await Promise.all(jobs);assert.deepEqual(sent,['link','new gain']);
});

test('a failed request settles superseded callers and does not strand later controls', async () => {
  const queue=new LatestWriteQueue(0), first=gate();
  const start=queue.enqueue('first',async()=>first.promise);
  const old=queue.enqueue('failed',async()=>0).catch(e=>e.message);
  const latest=queue.enqueue('failed',async()=>{throw new Error('disconnected');}).catch(e=>e.message);
  const next=queue.enqueue('next',async()=>42);first.resolve();await start;
  assert.equal(await old,'disconnected');assert.equal(await latest,'disconnected');assert.equal(await next,42);
  await queue.idle();assert.equal(queue.running,false);
});

test('the final value is sent even when the drag ends during a pending request', async () => {
  const queue=new LatestWriteQueue(0), first=gate(), second=gate(), sent=[];
  const a=queue.enqueue('gain',async()=>{sent.push(1);await first.promise;});
  const b=queue.enqueue('gain',async()=>{sent.push(2);await second.promise;});
  first.resolve();await a;
  const c=queue.enqueue('gain',async()=>sent.push(3));second.resolve();await Promise.all([b,c]);
  assert.deepEqual(sent,[1,2,3]);
});

test('idle waits for the complete queue and a later interaction starts a new drain', async () => {
  const queue=new LatestWriteQueue(0), first=gate();let idle=false;
  const job=queue.enqueue('one',async()=>first.promise);const waiting=queue.idle().then(()=>idle=true);
  await Promise.resolve();assert.equal(idle,false);first.resolve();await job;await waiting;
  assert.equal(await queue.enqueue('two',async()=>2),2);
});
