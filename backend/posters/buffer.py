import os
import requests
import datetime
from pathlib import Path
from ..config import get_settings

def _clean_token(access_token: str) -> str:
    t = str(access_token or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t

def _get_graphql_profiles(token: str):
    """
    Fetches profiles using Buffer's modern GraphQL API (https://api.buffer.com).
    This endpoint supports modern channels including TikTok, YouTube Shorts, and Instagram Professional.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    org_query = """
    query GetOrganizations {
      account {
        id
        email
        organizations {
          id
          name
        }
      }
    }
    """
    try:
        r = requests.post("https://api.buffer.com", json={"query": org_query}, headers=headers, timeout=12)
        if r.status_code == 200:
            res_json = r.json()
            if res_json.get("errors"):
                err_msg = "; ".join([e.get("message", "") for e in res_json["errors"]])
                return [], err_msg
            account = res_json.get("data", {}).get("account")
            if not account:
                return [], "No account found in Buffer response"
            orgs = account.get("organizations") or []
            channels = []
            for org in orgs:
                org_id = org.get("id")
                if not org_id:
                    continue
                chan_query = """
                query GetChannels($input: ChannelsInput!) {
                  channels(input: $input) {
                    id
                    name
                    displayName
                    service
                    avatar
                    isQueuePaused
                  }
                }
                """
                r_chan = requests.post(
                    "https://api.buffer.com",
                    json={"query": chan_query, "variables": {"input": {"organizationId": org_id}}},
                    headers=headers,
                    timeout=12
                )
                if r_chan.status_code == 200:
                    chan_json = r_chan.json()
                    c_list = chan_json.get("data", {}).get("channels") or []
                    for ch in c_list:
                        channels.append({
                            "id": str(ch.get("id")),
                            "service": str(ch.get("service") or "").lower(),
                            "name": ch.get("displayName") or ch.get("name") or ch.get("service"),
                            "service_username": ch.get("name") or ch.get("displayName") or "",
                            "api_type": "graphql",
                            "organization_id": org_id,
                        })
            return channels, None
        elif r.status_code in (401, 403):
            return [], f"Buffer rejected token (HTTP {r.status_code})"
        return [], f"Buffer GraphQL error (HTTP {r.status_code})"
    except Exception as e:
        return [], f"GraphQL request failed: {e}"

def _get_rest_profiles(token: str):
    """Fetches profiles using Buffer's legacy REST API (https://api.bufferapp.com/1/profiles.json)."""
    try:
        r = requests.get(
            "https://api.bufferapp.com/1/profiles.json",
            params={"access_token": token},
            timeout=10,
        )
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                return [], f"Buffer returned invalid JSON: {r.text[:150]}"
            if isinstance(data, list):
                profiles = []
                for p in data:
                    profiles.append({
                        "id": str(p.get("id")),
                        "service": str(p.get("service") or "").lower(),
                        "name": p.get("formatted_username") or p.get("service_username") or p.get("service"),
                        "service_username": p.get("service_username") or "",
                        "api_type": "rest",
                    })
                return profiles, None
            return [], f"Unexpected Buffer response: {str(data)[:150]}"
        body = " ".join((r.text or "").split())[:200]
        if r.status_code in (401, 403):
            return [], f"Buffer rejected the access token (HTTP {r.status_code}: {body})"
        return [], f"Buffer API error (HTTP {r.status_code}): {body}"
    except Exception as e:
        return [], f"Could not reach Buffer: {e}"

def get_buffer_profiles(access_token: str, cached_profiles: list = None):
    """
    Fetches all social channels connected to the user's Buffer account.
    First checks modern GraphQL API (which has TikTok, YouTube, Instagram Reels).
    If no channels found, falls back to legacy REST profiles.
    If network query fails or returns empty, falls back to cached_profiles.
    """
    token = _clean_token(access_token)
    if not token:
        return [], "No token provided"
        
    # 1. Try GraphQL first (modern accounts with TikTok, YouTube, IG Reels)
    gql_profiles, gql_err = _get_graphql_profiles(token)
    if gql_profiles:
        return gql_profiles, None
        
    # 2. Try legacy REST fallback
    rest_profiles, rest_err = _get_rest_profiles(token)
    if rest_profiles:
        return rest_profiles, None

    # 3. Fallback to cached profiles if available
    if cached_profiles and isinstance(cached_profiles, list):
        return cached_profiles, None
        
    # Both failed or returned empty
    err = gql_err or rest_err
    if err and ("rejected" in err.lower() or "401" in err or "403" in err):
        return [], "Buffer rejected the access token. Please verify your token in Settings."
        
    return [], (
        "No channels are connected inside your Buffer account. "
        "Add Instagram, TikTok, or YouTube in your Buffer dashboard at buffer.com/channels."
    )

def _post_graphql_channel(token: str, channel_id: str, text: str, video_url: str, schedule_timestamp: int = None):
    """Creates a post via Buffer GraphQL createPost mutation."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess {
          post {
            id
            text
          }
        }
        ... on MutationError {
          message
        }
      }
    }
    """
    
    # Try shareNow first (or customScheduled if scheduled)
    mode = "shareNow" if not schedule_timestamp else "customScheduled"
    post_input = {
        "channelId": channel_id,
        "text": text,
        "schedulingType": "automatic",
        "mode": mode,
        "assets": [
            {
                "video": {
                    "url": video_url
                }
            }
        ]
    }
    if schedule_timestamp:
        post_input["dueAt"] = datetime.datetime.fromtimestamp(schedule_timestamp, datetime.timezone.utc).isoformat()

    try:
        r = requests.post("https://api.buffer.com", json={"query": mutation, "variables": {"input": post_input}}, headers=headers, timeout=25)
        res = r.json() if r.status_code == 200 else {}
        data = res.get("data", {}).get("createPost", {})
        
        # Check for success
        if data.get("post"):
            return True, data["post"].get("id"), None
            
        error_msg = data.get("message") or str(res.get("errors") or r.text[:200])
        
        # If shareNow is not supported for this channel or plan, fallback to addToQueue
        if mode == "shareNow" and any(k in str(error_msg).lower() for k in ["sharenow", "mode", "queue", "plan", "support"]):
            post_input["mode"] = "addToQueue"
            r_retry = requests.post("https://api.buffer.com", json={"query": mutation, "variables": {"input": post_input}}, headers=headers, timeout=25)
            res_retry = r_retry.json() if r_retry.status_code == 200 else {}
            data_retry = res_retry.get("data", {}).get("createPost", {})
            if data_retry.get("post"):
                return True, data_retry["post"].get("id"), None
            error_msg = data_retry.get("message") or error_msg
            
        return False, None, error_msg
    except Exception as e:
        return False, None, str(e)

def _post_rest_profiles(token: str, profile_ids: list, text: str, video_url: str, schedule_timestamp: int = None):
    endpoint = "https://api.bufferapp.com/1/updates/create.json"
    payload = {
        "access_token": token,
        "profile_ids[]": profile_ids,
        "text": text,
        "media[video]": video_url,
        "now": "true" if not schedule_timestamp else "false"
    }
    if schedule_timestamp:
        payload["scheduled_at"] = schedule_timestamp
    try:
        r = requests.post(endpoint, data=payload, timeout=20)
        res = r.json()
        if res.get("success"):
            return True, res.get("updates", []), None
        return False, [], str(res)
    except Exception as e:
        return False, [], str(e)

def post_to_buffer(token: str, video_url: str, caption: str, hashtags: list,
                   schedule_timestamp: int = None, exclude_services: list = None,
                   cached_profiles: list = None):
    """
    Schedules/Posts a video clip to all connected channels on Buffer (Instagram, TikTok, YouTube, etc.).
    Supports both modern GraphQL channels and legacy REST channels.
    `token` is the user's Buffer access token (from their connected account).
    """
    access_token = _clean_token(token)
    if not access_token:
        return {"success": False, "error": "No Buffer access token configured"}

    profiles, prof_err = get_buffer_profiles(access_token, cached_profiles=cached_profiles)
    if not profiles:
        return {"success": False, "error": prof_err or "No connected channels found on your Buffer account"}

    if exclude_services:
        profiles = [
            p for p in profiles
            if not any(s in str(p.get("service", "")).lower() or s in str(p.get("service_username", "")).lower()
                       for s in exclude_services)
        ]
        if not profiles:
            return {"success": False, "error": "All matching Buffer channels were excluded by your settings"}

    full_text = caption + "\n\n" + (" ".join(hashtags) if isinstance(hashtags, list) else (hashtags or ""))
    
    # Split by api_type
    gql_profiles = [p for p in profiles if p.get("api_type") == "graphql"]
    rest_profiles = [p for p in profiles if p.get("api_type") != "graphql"]
    
    succeeded_channels = []
    failed_channels = []
    
    # 1. Post to GraphQL channels
    for ch in gql_profiles:
        ch_id = ch["id"]
        ch_name = (ch.get("service") or ch.get("name") or "channel").capitalize()
        ok, post_id, err = _post_graphql_channel(access_token, ch_id, full_text, video_url, schedule_timestamp)
        if ok:
            succeeded_channels.append(ch_name)
        else:
            failed_channels.append(f"{ch_name}: {err}")
            
    # 2. Post to REST channels
    if rest_profiles:
        profile_ids = [p["id"] for p in rest_profiles]
        ok, updates, err = _post_rest_profiles(access_token, profile_ids, full_text, video_url, schedule_timestamp)
        if ok:
            for p in rest_profiles:
                succeeded_channels.append((p.get("service") or p.get("name") or "channel").capitalize())
        else:
            failed_channels.append(f"Legacy Buffer channels: {err}")
            
    if succeeded_channels:
        return {
            "success": True,
            "message": f"Successfully queued to {', '.join(succeeded_channels)} on Buffer!",
            "channels": succeeded_channels,
            "warnings": failed_channels if failed_channels else None
        }
        
    return {
        "success": False,
        "error": "; ".join(failed_channels) or "Failed to post to Buffer channels"
    }
