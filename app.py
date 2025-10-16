from fastapi import FastAPI, Request, Response
from twilio.twiml.voice_response import VoiceResponse

app = FastAPI()

@app.get("/")
def health():
    return {"status": "ok"}

def twiml_response():
    vr = VoiceResponse()
    vr.say("Welkom bij Ristorante Adam. Testopstelling actief. Fijne dag verder.")
    return Response(str(vr), media_type="application/xml")

@app.get("/twilio/voice")
async def test_voice():
    return twiml_response()

@app.post("/twilio/voice")
async def voice(_: Request):
    return twiml_response()
