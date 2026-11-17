for (const u of ['https://www.estlatbl.com/et/tulemused/2022212804/','https://www.estlatbl.com/et/tulemused/2022212804/live-stats']) {
  try {
    const r=await fetch(u,{signal:AbortSignal.timeout(15000)}); const t=await r.text();
    console.log(JSON.stringify({url:u,status:r.status,bytes:t.length,title:(t.match(/<title>(.*?)<\/title>/is)?.[1]??null),checkpoint:/Turvakontroll|turnstile-guard/i.test(t),matchIds:[...t.matchAll(/matchId=(\d+)/g)].map(m=>m[1])}));
  } catch(e) { console.log(JSON.stringify({url:u,error:e.cause?.code??e.message})); }
}
