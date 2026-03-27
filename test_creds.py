from app import load_env_or_secret, GCP_PROJECT_ID

print("Testing credential loading...")
print(f"GCP Project: {GCP_PROJECT_ID}")

try:
    client_id = load_env_or_secret("AMAZON_ADS_CLIENT_ID")
    print(f"✓ Client ID loaded: {client_id[:10]}...")
    
    client_secret = load_env_or_secret("AMAZON_ADS_CLIENT_SECRET")
    print(f"✓ Client Secret loaded: {client_secret[:10]}...")
    
    refresh_token = load_env_or_secret("AMAZON_ADS_REFRESH_TOKEN")
    print(f"✓ Refresh Token loaded: {refresh_token[:10]}...")
    
    profile_id = load_env_or_secret("AMAZON_ADS_PROFILE_ID")
    print(f"✓ Profile ID: {profile_id}")
    
    print("\n✅ All credentials loaded successfully!")
    print("\nNow testing token exchange...")
    
    import requests
    
    TOKEN_URL = "https://api.amazon.com/auth/o2/token"
    
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret
    }
    
    resp = requests.post(TOKEN_URL, data=data)
    print(f"Token request status: {resp.status_code}")
    
    if resp.status_code == 200:
        print("✅ Token exchange successful!")
    else:
        print(f"❌ Token exchange failed: {resp.text}")
        
except Exception as e:
    print(f"❌ Error: {e}")
