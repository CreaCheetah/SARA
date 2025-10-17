// Mada Belbot – Twilio Voice webhook (no forwarding; time-based closed greeting)

const express = require('express');
const bodyParser = require('body-parser');
const { twiml: { VoiceResponse } } = require('twilio');
const crypto = require('crypto');

// Config
const PORT = process.env.PORT || 3000;
const TZ = 'Europe/Amsterdam';
const TWILIO_AUTH_TOKEN = process.env.TWILIO_AUTH_TOKEN || null; // optioneel
const MADA_LIVE = process.env.MADA_LIVE === 'true'; // laat op false

// Operationele vensters (projectcontext)
// Overdag (16:00–18:00), Avond (18:00–21:30), Laat (21:30–22:00 alleen afhalen)
const WINDOW_OPEN_START = { h:16, m:0 };
const WINDOW_DINNER_END = { h:21, m:30 };
const WINDOW_TAKEOUT_END = { h:22, m:0 };

// Helpers
function nowInAmsterdam() {
  const utc = new Date();
  const fmt = new Intl.DateTimeFormat('nl-NL', {
    timeZone: TZ, hour12: false, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit'
  });
  const parts = Object.fromEntries(fmt.formatToParts(utc).map(p => [p.type, p.value]));
  return new Date(parseInt(parts.year,10), parseInt(parts.month,10)-1, parseInt(parts.day,10),
                  parseInt(parts.hour,10), parseInt(parts.minute,10), parseInt(parts.second,10));
}
function hm(d){return d.getHours()*60+d.getMinutes();}
function minutes(h,m){return h*60+m;}
function partOfDay(d){const h=d.getHours(); if(h<6)return'nacht'; if(h<12)return'morgen'; if(h<18)return'middag'; return'avond';}
function salutation(d){const p=partOfDay(d); if(p==='morgen')return'Goedemorgen'; if(p==='middag')return'Goedemiddag'; return'Goedenavond';}
function isWithin(d,a,b){const t=hm(d); return t>=minutes(a.h,a.m) && t<minutes(b.h,b.m);}
function statusFor(d){
  const open = isWithin(d, WINDOW_OPEN_START, WINDOW_TAKEOUT_END);
  const dinner = isWithin(d, WINDOW_OPEN_START, WINDOW_DINNER_END);
  const takeout = isWithin(d, WINDOW_DINNER_END, WINDOW_TAKEOUT_END);
  return { open, dinner, takeout };
}
function validateTwilioSignature(req){
  if(!TWILIO_AUTH_TOKEN) return true; // uit in dev
  const url = (req.protocol+'://'+req.get('host')+req.originalUrl);
  const params = req.body || {};
  const sorted = Object.keys(params).sort().reduce((acc,k)=>acc+k+params[k],'');
  const base = url + sorted;
  const sig = crypto.createHmac('sha1', TWILIO_AUTH_TOKEN).update(Buffer.from(base,'utf-8')).digest('base64');
  return sig === req.get('X-Twilio-Signature');
}

// App
const app = express();
app.use(bodyParser.urlencoded({ extended: false }));

app.get('/runtime/status', (req,res)=>{
  const now = nowInAmsterdam();
  res.json({ now: now.toISOString(), tz: TZ, partOfDay: partOfDay(now), status: statusFor(now), live: MADA_LIVE });
});

app.post('/voice/incoming', (req, res) => {
  if (!validateTwilioSignature(req)) return res.status(403).send('Forbidden');

  const vr = new VoiceResponse();
  const now = nowInAmsterdam();
  const greet = salutation(now);
  const st = statusFor(now);

  // Voor nu: nooit doorverbinden. Nummers bewust weggelaten.
  vr.say({ language: 'nl-NL' }, `${greet}. We zijn op dit moment gesloten.`);
  vr.pause({ length: 1 });

  if (st.open) {
    if (st.dinner) vr.say({ language: 'nl-NL' }, 'Keuken geopend van zestien tot eenentwintig uur dertig.');
    else if (st.takeout) vr.say({ language: 'nl-NL' }, 'Alleen afhalen mogelijk tot tweeëntwintig uur.');
  } else {
    vr.say({ language: 'nl-NL' }, 'Onze tijden: diner van zestien tot eenentwintig uur dertig, afhalen tot tweeëntwintig uur.');
  }

  vr.say({ language: 'nl-NL' }, 'Bezoek onze website voor het menu. Dank u wel en een prettige dag.');
  vr.hangup();

  res.type('text/xml').send(vr.toString());
});

app.listen(PORT, ()=>{ console.log(`Mada Voice webhook listening on :${PORT}`); });
