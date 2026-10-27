export const DEFAULT_PROXY_ADMIN_USER_ID = "default_user_id";

export const UI_TEAM_ID = "litellm-dashboard";

/** Proxy admins must not appear as org/team members in the UI. */
export const isHiddenProxyMembership = (member: {
  user_id?: string | null;
  user_role?: string | null;
  role?: string | null;
}): boolean => {
  if (member.user_id === DEFAULT_PROXY_ADMIN_USER_ID) {
    return true;
  }
  const role = member.user_role ?? member.role;
  return role === "proxy_admin" || role === "proxy_admin_viewer";
};
