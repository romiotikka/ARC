const j=await (await fetch('https://fibalivestats.dcd.shared.geniussports.com/data/1999499/data.json')).json();console.log(JSON.stringify(j.othermatches));
