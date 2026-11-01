
import os
from google import genai
from google.genai import errors

key = os.getenv("GEMINI_API_KEY")
if not key:
    raise SystemExit("GEMINI_API_KEY is not set in this terminal.")

print("API key is configured. Testing Gemini...")

try:
    with genai.Client(api_key=key) as client:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents="Reply with exactly: Gemini API works."
        )

        print("\n--- Gemini response ---")
        print(response.text)
        print("\n--- Response metadata ---")
        print(response.model_dump_json(indent=2))

except errors.APIError as exc:
    print("\n--- Gemini API error ---")
    print("HTTP status:", exc.code)
    print("Message:", exc.message)

except Exception as exc:
    print("\n--- Client or network error ---")
    print(type(exc).__name__ + ":", str(exc))
