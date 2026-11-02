'use strict';

// One request at a time. While a request is running, replace unsent updates to
// the same control with its newest value. Every caller settles with the update
// that superseded it; a failed request never strands the rest of the queue.
class LatestWriteQueue {
  constructor(interval = 40) {
    this.interval = interval;
    this.pending = new Map();
    this.running = false;
    this.lastStarted = 0;
    this.waiters = [];
  }
  enqueue(key, run) {
    const promise = new Promise((resolve, reject) => {
      const previous = this.pending.get(key);
      this.pending.delete(key);
      this.pending.set(key, {run, callers: [...(previous?.callers || []), {resolve, reject}]});
    });
    this.drain();
    return promise;
  }
  async drain() {
    if (this.running) return;
    this.running = true;
    try {
      while (this.pending.size) {
        const delay = this.interval - (Date.now() - this.lastStarted);
        if (delay > 0) await new Promise(resolve => setTimeout(resolve, delay));
        const [key, job] = this.pending.entries().next().value;
        this.pending.delete(key);
        this.lastStarted = Date.now();
        try {
          const result = await job.run();
          job.callers.forEach(caller => caller.resolve(result));
        } catch (error) {
          job.callers.forEach(caller => caller.reject(error));
        }
      }
    } finally {
      this.running = false;
      this.waiters.splice(0).forEach(resolve => resolve());
    }
  }
  idle() {
    return this.running ? new Promise(resolve => this.waiters.push(resolve)) : Promise.resolve();
  }
}

if (typeof module !== 'undefined') module.exports = {LatestWriteQueue};
