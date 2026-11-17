const t=await(await fetch('https://fibalivestats.dcd.shared.geniussports.com/u/EBF/1999499/p.html')).text();
for(const key of ['Competition','Game Details','Tip off']){const i=t.indexOf(key);console.log(key,i,t.slice(i-200,i+350).replace(/\s+/g,' '));}
