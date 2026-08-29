import { useAuth } from "@clerk/clerk-react";
import { useEffect } from "react";
import { setAuthTokenGetter } from "../api/client";

/**
 * Registers the API client's auth-token getter once Clerk's hooks are
 * available, so every request.ts call automatically attaches
 * `Authorization: Bearer <token>` for a signed-in user, exactly like the
 * previous vanilla app.js's getAuthToken() did -- the backend already
 * prefers this over the anonymous X-User-Id header when both are present
 * (see auth.resolve_user_id in main.py).
 */
export function ClerkTokenBridge() {
  const { getToken, isSignedIn } = useAuth();

  useEffect(() => {
    setAuthTokenGetter(async () => {
      if (!isSignedIn) return null;
      try {
        return await getToken();
      } catch {
        return null;
      }
    });
  }, [getToken, isSignedIn]);

  return null;
}
