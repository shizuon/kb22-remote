// Deterministic workload, simulated 70 ms request latency; no USB or browser.
const {LatestWriteQueue} = require('../write_queue.js');
const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));
async function experiment(strategy, events) {
  const start=performance.now(), stepMs=10, requestMs=70;
  const sent=[], finished=[], promises=[];let busy=false,tail=Promise.resolve();
  const queue=new LatestWriteQueue(40);
  async function send(value){sent.push(value);await sleep(requestMs);finished.push(value);}
  for(let value=0;value<events;value++) {
    await sleep(Math.max(0,start+value*stepMs-performance.now()));
    if(strategy==='drop_while_busy') {
      if(!busy){busy=true;promises.push(send(value).finally(()=>busy=false));}
    } else if(strategy==='fifo') {tail=tail.then(()=>send(value));}
    else promises.push(queue.enqueue('gain',()=>send(value)));
  }
  const dragEnded=performance.now();await Promise.all(promises);await tail;await queue.idle();
  return {strategy,events,requestMs,stepMs,requests:sent.length,finalValue:finished.at(-1),expectedFinal:events-1,
    finalValuePreserved:finished.at(-1)===events-1,settleAfterDragMs:Math.round(performance.now()-dragEnded),totalMs:Math.round(performance.now()-start)};
}
Promise.all([21,51,101].flatMap(events=>['drop_while_busy','fifo','latest'].map(strategy=>experiment(strategy,events)))).then(results=>console.log(JSON.stringify({kind:'simulated-request-ablation',results},null,2)));
