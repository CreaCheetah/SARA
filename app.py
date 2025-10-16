from fastapi import FastAPI, Request, Response
from twilio.twiml.voice_response import VoiceResponse

app = FastAPI()

@app.get("/")
def health():
    return {"status": "ok"}

@app.post("/twilio/voice")
async def voice(_: Request):
    vr = VoiceResponse()
    vr.say("Welkom bij Ristorante Adam. Testopstelling actief.")
    return Response(str(vr), media_type="application/xml")
