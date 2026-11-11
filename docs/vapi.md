# Call Switchboard through Vapi

Vapi provides the number and handles transcription and speech. Switchboard handles the conversation and uses the same calendar, confirmations, email outbox, and staff work queue as the browser demo. The assistant configuration uses Deepgram Nova 3 and Helena at 0.9× speed. Vapi can supply both speech services through its own integration.

Use fictional patient details. Vapi receives the conversation, and this is a synthetic clinic demo. The supplied configuration turns off recordings, transcript artifacts, and SIP packet captures. Vapi's free U.S. number accepts inbound calls. Calls and speech use Vapi credits. Check your balance before testing. [Vapi phone numbers](https://docs.vapi.ai/free-telephony) · [Vapi pricing](https://vapi.ai/pricing)

## Start Switchboard and its tunnel

1. From the project directory, run the following in Terminal. Replace the example number with the cell number you'll dial **from**, including the country code. Save the generated secret in your password manager. Use the same secret when you restart the server and when you configure Vapi in step 3. The command uses a separate local demo calendar. If you want to reuse a calendar, substitute its database path and run only one instance against it.

   ```zsh
   export SWITCHBOARD_VAPI_SECRET="$(openssl rand -hex 32)"
   export SWITCHBOARD_VAPI_CALLERS="+12025550123"
   export SWITCHBOARD_OPERATIONS_TOKEN="$(openssl rand -hex 24)"
   export SWITCHBOARD_STAFF_TRANSCRIPTS=1
   export SWITCHBOARD_DB="$PWD/switchboard-vapi.sqlite"
   print -r -- "$SWITCHBOARD_VAPI_SECRET"
   print -r -- "$SWITCHBOARD_OPERATIONS_TOKEN"
   uv run uvicorn switchboard.api:app --host 127.0.0.1 --port 8766 --workers 1
   ```

   Keep the process running. This uses port 8766 so it can run alongside the existing demo on 8765. The adapter keeps active call state in memory, so use one worker and avoid restarting during a call. When restarting from the same terminal, repeat just the last command to reuse the same settings.

2. Install and sign in to ngrok, then start the tunnel in a second terminal from the project directory. Use the `https://…` forwarding URL it prints. The supplied policy exposes only the two Vapi endpoints and leaves the browser call lab and Operations local. [ngrok setup](https://ngrok.com/docs/getting-started)

   ```sh
   ngrok http 8766 --inspect=false --traffic-policy-file docs/vapi-ngrok.yml
   ```

## Create the Vapi assistant and number

3. In the [Vapi dashboard](https://dashboard.vapi.ai), copy your **private API key**. In a terminal in the project directory, use it to create a saved assistant with the configuration below. The command prompts for your tunnel URL, the Vapi key, and the **Switchboard Vapi secret** from step 1. It requires `jq` and runs in zsh. Run it once. If your tunnel URL changes later, update the same assistant's model URL and webhook URL in Vapi.

   ```zsh
   read "switchboard_tunnel?Public HTTPS tunnel URL: "
   read -s "switchboard_vapi_key?Vapi private API key: "
   print
   read -s "switchboard_vapi_secret?Switchboard Vapi secret: "
   print
   jq --arg url "${switchboard_tunnel%/}" --arg secret "$switchboard_vapi_secret" '
     .model.url = ($url + "/vapi") |
     .server.url = ($url + "/vapi/events") |
     .model.headers["X-Switchboard-Vapi-Secret"] = $secret |
     .server.headers["X-Switchboard-Vapi-Secret"] = $secret
   ' docs/vapi-assistant.json |
     curl --silent --show-error --fail-with-body https://api.vapi.ai/assistant \
       -H "Authorization: Bearer $switchboard_vapi_key" \
       -H 'Content-Type: application/json' --data-binary @- |
     jq 'if .id then {id, name} else {statusCode, error, message} end'
   unset switchboard_vapi_key switchboard_vapi_secret switchboard_tunnel
   ```

   The model base URL ends in `/vapi`. Vapi appends `/chat/completions`. The separate `/vapi/events` endpoint receives playback and hangup events. Keep the configured server messages enabled so Switchboard can verify that a readback finished before accepting confirmation. [Custom model setup](https://docs.vapi.ai/customization/custom-llm/using-your-server) · [Vapi server events](https://docs.vapi.ai/server-url/events)

4. In **Phone Numbers → Create Phone Number → Free Vapi Number**, choose a U.S. area code. Under **Inbound Settings**, select **Switchboard phone demo** and save. Once the number activates, dial it from the cell number in step 1 with caller ID enabled. The dashboard's browser **Talk** button uses a web call, so use your phone for this test.

5. Say “Book a follow-up this Saturday.” You can use **Maria Lopez**, born **July 2, 1984**, or use a fictional name and date of birth to book a first visit. Confirm the readback, choose an option, and confirm the final time after Helena finishes speaking. Hang up after hearing the result. Open [local Operations](http://127.0.0.1:8766/operations) and use the operations token from step 1 to check the call outcome and email outbox. Ask for the front desk in a separate call to test the staff work queue.

If you hear the greeting but the call won't continue, check that the tunnel is running and the configured model URL ends in `/vapi`. A 401 in the server log means the Vapi request's secret header and the app secret differ. If a readback repeats after you confirm, check that the assistant is sending its playback events to `/vapi/events` with the same secret, then wait for Helena to finish before confirming again.

## Refresh speech settings on an existing assistant

Restart Switchboard in its original terminal to pick up app code changes. Reuse the same secret and port. To apply the updated voice and transcription settings to an assistant you already assigned to a phone number, copy its ID from the assistant page and run this from the project directory in zsh. This updates the saved assistant in place.

```zsh
read "switchboard_assistant_id?Vapi assistant ID: "
read -s "switchboard_vapi_key?Vapi private API key: "
print
jq '{transcriber, voice, startSpeakingPlan, stopSpeakingPlan}' docs/vapi-assistant.json |
  curl --silent --show-error --fail-with-body -X PATCH \
    "https://api.vapi.ai/assistant/$switchboard_assistant_id" \
    -H "Authorization: Bearer $switchboard_vapi_key" \
    -H 'Content-Type: application/json' --data-binary @- |
  jq 'if .id then {id, name} else {statusCode, error, message} end'
unset switchboard_vapi_key switchboard_assistant_id
```

## Review appointments and calls in Operations

Use **Appointments** to find visits by clinic date, doctor, and status. Staff can cancel eligible visits or reschedule them with the same doctor. Rescheduling creates an updated email confirmation in the outbox.

For a linked booking, select **View activity → View conversation**. The local simulator can save caller text and assistant replies for seven days when `SWITCHBOARD_STAFF_TRANSCRIPTS=1` and an Operations token are set. This shows the text received or generated by Switchboard, including a list of questions. Calls made before capture was enabled show only their saved activity.
