
from openai import OpenAI

# Configure the gateway client
client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sk-dummy-key" # Gateway might require a key even if dummy
)

# Make a request
try:
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "Hello"}]
    )
    print(response.choices[0].message.content)
except Exception as e:
    print(f"Error: {e}")
