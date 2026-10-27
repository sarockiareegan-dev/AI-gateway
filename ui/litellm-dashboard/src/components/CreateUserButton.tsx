import { brandDocsUrl } from "@/lib/brand";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useOrganizations } from "@/app/(dashboard)/hooks/organizations/useOrganizations";
import { MultiSelect } from "@/components/shared/MultiSelect";
import { FieldGroup } from "@/components/ui/field";
import { FormField } from "@/components/shared/form/FormField";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { ChevronRight, CircleHelp, Info, UserPlus } from "lucide-react";
import React, { useEffect, useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import TeamDropdown from "./common_components/team_dropdown";
import { getModelDisplayName } from "./key_team_helpers/fetch_available_models_team_key";
import { toast } from "@/lib/toast";
import {
  getProxyBaseUrl,
  getProxyUISettings,
  invitationCreateCall,
  modelAvailableCall,
  userCreateCall,
  getInviteOptionsCall,
  InviteOptionsResponse,
} from "./networking";
import OnboardingModal, { InvitationLink } from "./onboarding_link";

// Helper function to generate UUID compatible across all environments
const generateUUID = (): string => {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  // Fallback UUID generation for environments without crypto.randomUUID
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
    const r = (Math.random() * 16) | 0;
    const v = c == "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
};

interface CreateuserProps {
  userID: string;
  accessToken: string;
  possibleUIRoles: null | Record<string, Record<string, string>>;
  onUserCreated?: (userId: string) => void;
  isEmbedded?: boolean;
  inviteOptions?: InviteOptionsResponse | null;
}

// Define an interface for the UI settings
interface UISettings {
  PROXY_BASE_URL: string | null;
  PROXY_LOGOUT_URL: string | null;
  DEFAULT_TEAM_DISABLED: boolean;
  SSO_ENABLED: boolean;
}

interface CreateUserFormValues {
  user_email?: string;
  user_role: string;
  team_id?: string | null;
  organization_ids?: string[];
  metadata?: string;
  send_invite_email: boolean;
  models?: string[];
  team_role?: "admin" | "user";
  org_role?: "org_admin" | "internal_user" | "viewer";
}

const AGAMI_GLOBAL_ROLE_META: Record<string, { ui_label: string; description: string }> = {
  proxy_admin: {
    ui_label: "Proxy Admin",
    description: "Full platform access; not added to any organization or team",
  },
  internal_user: {
    ui_label: "Member",
    description: "Standard user; must belong to an organization and related team",
  },
  viewer: {
    ui_label: "Viewer",
    description: "Same scope as member, read-only (cannot invite or edit)",
  },
};

const EMBEDDED_DEFAULTS: CreateUserFormValues = {
  user_email: undefined,
  user_role: "internal_user",
  team_id: undefined,
  metadata: undefined,
  send_invite_email: true,
};

const STANDALONE_DEFAULTS: CreateUserFormValues = {
  user_email: undefined,
  user_role: "internal_user",
  team_id: undefined,
  organization_ids: undefined,
  metadata: undefined,
  send_invite_email: true,
};

// antd reported only mounted fields, so a collapsed Personal Key Creation
// section left `models` out of the request entirely even after the admin had
// picked some and closed it again.
const withoutCollapsedModels = (values: CreateUserFormValues, isPersonalKeyOpen: boolean): CreateUserFormValues => {
  if (isPersonalKeyOpen) {
    return values;
  }
  const { models, ...rest } = values;
  return rest;
};

const needsDefaultModels = (values: CreateUserFormValues): boolean => {
  const hasModels = Boolean(values.models && values.models.length > 0);
  return !hasModels && values.user_role !== "proxy_admin";
};

const hasOrgRoleAssignment = (values: CreateUserFormValues): boolean => {
  const hasOrgs = Boolean(values.organization_ids && values.organization_ids.length > 0);
  return hasOrgs && Boolean(values.org_role);
};

const buildCreatePayload = (values: CreateUserFormValues): CreateUserFormValues & { organizations?: string[] } => {
  const withModels = needsDefaultModels(values) ? { ...values, models: ["no-default-models"] } : { ...values };

  if (!withModels.team_id || !withModels.team_role) {
    delete (withModels as any).team_role;
  }
  if (!hasOrgRoleAssignment(withModels)) {
    delete (withModels as any).org_role;
  }

  if (!withModels.organization_ids) {
    return withModels;
  }
  const { organization_ids, ...rest } = withModels;
  return { ...rest, organizations: organization_ids };
};

const SSO_INVITE_LIFETIME_MS = 7 * 24 * 60 * 60 * 1000;

const buildSsoInvitationLink = (userId: string, createdBy: string): InvitationLink => {
  const now = new Date();
  return {
    id: generateUUID(),
    user_id: userId,
    is_accepted: false,
    accepted_at: null,
    expires_at: new Date(now.getTime() + SSO_INVITE_LIFETIME_MS),
    created_at: now,
    created_by: createdBy,
    updated_at: now,
    updated_by: createdBy,
    has_user_setup_sso: true,
  };
};

const labelWithHint = (label: string, hint: string): React.ReactNode => (
  <>
    {label}
    <Tooltip>
      <TooltipTrigger render={<CircleHelp className="size-3.5 shrink-0 cursor-help text-muted-foreground" />} />
      <TooltipContent>{hint}</TooltipContent>
    </Tooltip>
  </>
);

const EmailInvitationsNotice: React.FC = () => (
  <Alert variant="info" className="mb-4">
    <Info />
    <AlertTitle>Email invitations</AlertTitle>
    <AlertDescription>
      New users receive an email invite only when an email integration (SMTP, Resend, or SendGrid) is configured.{" "}
      <a href={brandDocsUrl("https://docs.litellm.ai/docs/proxy/email")} target="_blank" rel="noreferrer">
        Learn how to set up email notifications
      </a>
    </AlertDescription>
  </Alert>
);

export const CreateUserButton: React.FC<CreateuserProps> = ({
  userID,
  accessToken,
  possibleUIRoles,
  onUserCreated,
  isEmbedded = false,
  inviteOptions: propInviteOptions,
}) => {
  const queryClient = useQueryClient();
  const inviteOptionsQueryOptions = {
    queryKey: ["inviteOptions", accessToken],
    queryFn: () => getInviteOptionsCall(accessToken),
    enabled: Boolean(accessToken) && propInviteOptions === undefined,
    staleTime: 60_000,
  };
  const { data: fetchedInviteOptions } = useQuery(inviteOptionsQueryOptions);
  const inviteOptions = propInviteOptions !== undefined ? propInviteOptions : fetchedInviteOptions;

  const [uiSettings, setUISettings] = useState<UISettings | null>(null);
  const defaultValues = isEmbedded ? EMBEDDED_DEFAULTS : STANDALONE_DEFAULTS;
  const form = useForm<CreateUserFormValues>({ defaultValues });
  const [isModalVisible, setIsModalVisible] = useState(false);
  const [apiuser, setApiuser] = useState<boolean>(false);
  const [userModels, setUserModels] = useState<string[]>([]);
  const [isPersonalKeyOpen, setIsPersonalKeyOpen] = useState(false);
  const [isInvitationLinkModalVisible, setIsInvitationLinkModalVisible] = useState(false);
  const [invitationLinkData, setInvitationLinkData] = useState<InvitationLink | null>(null);
  const [baseUrl, setBaseUrl] = useState<string | null>(null);

  const { data: organizations = [] } = useOrganizations();
  const filteredOrganizations = useMemo(() => {
    if (!inviteOptions) return organizations;
    if (inviteOptions.organization_ids && inviteOptions.organization_ids.includes("*")) {
      return organizations;
    }
    const allowedOrgs = new Set(inviteOptions.organization_ids || []);
    return organizations.filter((org) => org.organization_id && allowedOrgs.has(org.organization_id));
  }, [organizations, inviteOptions]);

  const organizationOptions = useMemo(
    () =>
      filteredOrganizations.map((org) => ({
        label: `${org.organization_alias} (${org.organization_id})`,
        value: org.organization_id ?? "",
      })),
    [filteredOrganizations],
  );

  const selectedOrgIds = form.watch("organization_ids");
  const selectedTeamId = form.watch("team_id");

  const canAssignTeamRole = !inviteOptions || Boolean(inviteOptions.team_roles?.includes("admin"));
  const showOrgPicker = !inviteOptions || (inviteOptions.organization_ids && inviteOptions.organization_ids.length > 0);
  const hasSelectedOrgs = Boolean(selectedOrgIds && selectedOrgIds.length > 0);
  const showOrgRoleField = showOrgPicker && hasSelectedOrgs;
  const teamPickerEnabled = hasSelectedOrgs || Boolean(isEmbedded);

  const teamAllowedByInviteOptions = (team: { team_id?: string; organization_id?: string | null }) => {
    if (!inviteOptions) {
      return true;
    }
    const teamIds = inviteOptions.team_ids;
    if (teamIds && !teamIds.includes("*") && (!team.team_id || !teamIds.includes(team.team_id))) {
      return false;
    }
    const orgIds = inviteOptions.organization_ids;
    const scopedOrgs = Boolean(orgIds && !orgIds.includes("*") && orgIds.length > 0);
    if (scopedOrgs && (!team.organization_id || !orgIds!.includes(team.organization_id))) {
      return false;
    }
    return true;
  };

  const teamMatchesSelectedOrgs = (team: { organization_id?: string | null }) => {
    if (!selectedOrgIds || selectedOrgIds.length === 0) {
      return true;
    }
    return Boolean(team.organization_id && selectedOrgIds.includes(team.organization_id));
  };

  const filterTeam = (team: any) => teamAllowedByInviteOptions(team) && teamMatchesSelectedOrgs(team);

  useEffect(() => {
    if (isModalVisible && inviteOptions) {
      const orgIds = inviteOptions.organization_ids;
      const singleOrgId = orgIds && orgIds.length === 1 && !orgIds.includes("*") ? orgIds[0] : undefined;
      if (singleOrgId && !form.getValues("organization_ids")) {
        form.setValue("organization_ids", [singleOrgId]);
      }
      const teamIds = inviteOptions.team_ids;
      const singleTeamId = teamIds && teamIds.length === 1 && !teamIds.includes("*") ? teamIds[0] : undefined;
      if (singleTeamId && !form.getValues("team_id") && (singleOrgId || hasSelectedOrgs)) {
        form.setValue("team_id", singleTeamId);
      }
      if (inviteOptions.global_roles?.length === 1 && form.getValues("user_role") === "internal_user") {
        form.setValue("user_role", inviteOptions.global_roles[0]);
      }
    }
  }, [isModalVisible, inviteOptions, form, hasSelectedOrgs]);

  useEffect(() => {
    if (!selectedOrgIds || selectedOrgIds.length === 0) {
      if (form.getValues("team_id")) {
        form.setValue("team_id", undefined);
      }
    }
  }, [selectedOrgIds, form]);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const userRole = "any";
        const modelDataResponse = await modelAvailableCall(accessToken, userID, userRole);
        const availableModels = [];
        for (let i = 0; i < modelDataResponse.data.length; i++) {
          const model = modelDataResponse.data[i];
          availableModels.push(model.id);
        }
        setUserModels(availableModels);
        const uiSettingsResponse = await getProxyUISettings(accessToken);
        setUISettings(uiSettingsResponse);
      } catch (error) {
        console.error("Error fetching model data:", error);
      }
    };

    setBaseUrl(getProxyBaseUrl());
    fetchData();
  }, []);

  const handleCancel = () => {
    setIsModalVisible(false);
    setApiuser(false);
    form.reset(defaultValues);
  };

  const handleCreate = async (formValues: CreateUserFormValues) => {
    try {
      toast.info("Making API Call");
      if (!isEmbedded) {
        setIsModalVisible(true);
      }
      const payload = buildCreatePayload(withoutCollapsedModels(formValues, isPersonalKeyOpen));
      const response = await userCreateCall(accessToken, null, payload);
      await queryClient.invalidateQueries({ queryKey: ["userList"] });
      setApiuser(true);
      const user_id = response.data?.user_id || response.user_id;

      if (onUserCreated && isEmbedded) {
        onUserCreated(user_id);
        form.reset(defaultValues);
        return;
      }

      if (!uiSettings?.SSO_ENABLED) {
        invitationCreateCall(accessToken, user_id).then((data) => {
          data.has_user_setup_sso = false;
          setInvitationLinkData(data);
          setIsInvitationLinkModalVisible(true);
        });
      } else {
        setInvitationLinkData(buildSsoInvitationLink(user_id, userID));
        setIsInvitationLinkModalVisible(true);
      }

      toast.success("API user Created");
      form.reset(defaultValues);
      localStorage.removeItem("userData" + userID);
    } catch (error: any) {
      const errorMessage = error.response?.data?.detail || error?.message || "Error creating the user";
      toast.fromError(errorMessage);
      console.error("Error creating the user:", error);
    }
  };

  const roleOptions = useMemo(() => {
    const allowedRoles =
      inviteOptions?.global_roles && inviteOptions.global_roles.length > 0
        ? inviteOptions.global_roles
        : Object.keys(AGAMI_GLOBAL_ROLE_META);
    return allowedRoles.map((role) => {
      const meta = AGAMI_GLOBAL_ROLE_META[role] ?? possibleUIRoles?.[role];
      return {
        value: role,
        label: meta?.ui_label ?? role,
        description: meta?.description ?? "",
      };
    });
  }, [possibleUIRoles, inviteOptions]);

  const orgRoleItems = useMemo(() => {
    const allowed = new Set(inviteOptions?.org_roles ?? ["org_admin", "internal_user", "viewer"]);
    return [
      { value: "internal_user", label: "Member (Default)", description: "Standard organization member" },
      {
        value: "org_admin",
        label: "Organization Admin",
        description: "Manages teams, members, and settings in the org",
      },
      { value: "viewer", label: "Viewer", description: "Read-only within the organization" },
    ].filter((item) => allowed.has(item.value));
  }, [inviteOptions]);

  const userEmailField = (
    <FormField control={form.control} name="user_email" label="User Email">
      {({ ref, value, ...control }) => <Input {...control} ref={ref} value={value ?? ""} />}
    </FormField>
  );

  const organizationField = showOrgPicker ? (
    <FormField
      control={form.control}
      name="organization_ids"
      label="Organization"
      description="Select the organization first. The team must belong to this organization."
    >
      {({ id, value, onChange }) => (
        <Select
          multiple
          items={organizationOptions}
          value={value ?? []}
          onValueChange={(selected: string[]) => {
            onChange(selected.length === 0 ? undefined : selected);
            form.setValue("team_id", undefined);
          }}
        >
          <SelectTrigger id={id} className="w-full">
            <SelectValue placeholder="Select Organization">
              {(selected: string[]) =>
                selected.length === 0
                  ? "Select Organization"
                  : organizationOptions
                      .filter((option) => selected.includes(option.value))
                      .map((option) => option.label)
                      .join(", ")
              }
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            {organizationOptions.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </FormField>
  ) : null;

  const teamField = (
    <FormField
      control={form.control}
      name="team_id"
      label="Team"
      description={
        teamPickerEnabled
          ? "Teams are limited to the selected organization."
          : "Select an organization before choosing a team."
      }
    >
      {({ id, value, onChange }) => (
        <TeamDropdown id={id} value={value} onChange={onChange} filterTeam={filterTeam} disabled={!teamPickerEnabled} />
      )}
    </FormField>
  );

  const teamRoleField = (
    <FormField
      control={form.control}
      name="team_role"
      label="Team Role"
      description="Role for this user within the selected team."
    >
      {({ id, value, onChange }) => (
        <Select
          items={[
            { value: "user", label: "Member (Default)" },
            { value: "admin", label: "Team Admin" },
          ]}
          value={value ?? ""}
          onValueChange={(selected: string | null) => onChange((selected as "admin" | "user") || undefined)}
        >
          <SelectTrigger id={id} className="w-full">
            <SelectValue placeholder="Member (Default)" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="user">
              <span>Member</span>
              <span className="ml-2 text-xs text-muted-foreground">Standard access to team models and keys</span>
            </SelectItem>
            <SelectItem value="admin">
              <span>Team Admin</span>
              <span className="ml-2 text-xs text-muted-foreground">Can approve key requests and manage team</span>
            </SelectItem>
          </SelectContent>
        </Select>
      )}
    </FormField>
  );

  const orgRoleField = (
    <FormField
      control={form.control}
      name="org_role"
      label="Organization Role"
      description="Role for this user within the selected organization(s)."
    >
      {({ id, value, onChange }) => (
        <Select
          items={orgRoleItems.map(({ value: v, label }) => ({ value: v, label }))}
          value={value ?? ""}
          onValueChange={(selected: string | null) =>
            onChange((selected as "org_admin" | "internal_user" | "viewer") || undefined)
          }
        >
          <SelectTrigger id={id} className="w-full">
            <SelectValue placeholder="Member (Default)" />
          </SelectTrigger>
          <SelectContent>
            {orgRoleItems.map((item) => (
              <SelectItem key={item.value} value={item.value}>
                <span>{item.label}</span>
                <span className="ml-2 text-xs text-muted-foreground">{item.description}</span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </FormField>
  );

  const metadataField = (
    <FormField control={form.control} name="metadata" label="Metadata">
      {({ ref, value, ...control }) => (
        <Textarea {...control} ref={ref} value={value ?? ""} rows={4} placeholder="Enter metadata as JSON" />
      )}
    </FormField>
  );

  const sendInviteEmailField = (
    <FormField control={form.control} name="send_invite_email" label="Send invitation email" orientation="horizontal">
      {({ id, value, onChange, onBlur }) => (
        <Checkbox id={id} checked={value} onCheckedChange={onChange} onBlur={onBlur} />
      )}
    </FormField>
  );

  const roleField = (label: React.ReactNode) => (
    <FormField control={form.control} name="user_role" label={label}>
      {({ id, value, onChange }) => (
        <Select
          items={roleOptions}
          value={value === undefined || value === "" ? null : value}
          onValueChange={(selected: string | null) => onChange(selected ?? undefined)}
        >
          <SelectTrigger id={id} className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {roleOptions.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                <span>{option.label}</span>
                <span className="ml-2 text-xs text-muted-foreground">{option.description}</span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </FormField>
  );

  // Modify the return statement to handle embedded mode
  if (isEmbedded) {
    return (
      <TooltipProvider>
        <form onSubmit={form.handleSubmit(handleCreate)}>
          <EmailInvitationsNotice />
          <FieldGroup>
            {userEmailField}
            {roleField("User Role")}
            {teamField}
            {selectedTeamId && canAssignTeamRole && teamRoleField}
            {metadataField}
            {sendInviteEmailField}
          </FieldGroup>
          <div className="mt-4 text-right">
            <Button type="submit">Create User</Button>
          </div>
        </form>
      </TooltipProvider>
    );
  }

  // Original return for standalone mode
  return (
    <>
      <Button type="button" onClick={() => setIsModalVisible(true)}>
        + Invite User
      </Button>
      <Dialog open={isModalVisible} onOpenChange={(open) => !open && handleCancel()}>
        <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-[800px]">
          <DialogHeader>
            <DialogTitle>Invite User</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-3">
            <p className="mb-1 text-sm text-foreground">Create a User who can own keys</p>
            <EmailInvitationsNotice />
          </div>
          <TooltipProvider>
            <form onSubmit={form.handleSubmit(handleCreate)}>
              <FieldGroup>
                {userEmailField}
                {organizationField}
                {showOrgRoleField && orgRoleField}
                {teamField}
                {selectedTeamId && canAssignTeamRole && teamRoleField}
                {roleField(
                  labelWithHint(
                    "Platform Role",
                    "Proxy Admin is platform-wide and is not added to any organization or team. Member and Viewer must belong to an organization and a related team.",
                  ),
                )}

                {metadataField}
                {sendInviteEmailField}

                <Collapsible open={isPersonalKeyOpen} onOpenChange={setIsPersonalKeyOpen}>
                  <CollapsibleTrigger className="flex w-full items-center gap-2 rounded-md border border-border px-3 py-2 text-left text-sm font-semibold text-foreground">
                    <ChevronRight
                      className={`size-4 transition-transform ${isPersonalKeyOpen ? "rotate-90" : ""}`}
                      aria-hidden
                    />
                    Personal Key Creation
                  </CollapsibleTrigger>
                  <CollapsibleContent className="pt-4">
                    <FormField
                      control={form.control}
                      name="models"
                      label={labelWithHint("Models", "Models user has access to, outside of team scope.")}
                      description="Models user has access to, outside of team scope."
                    >
                      {({ value, onChange }) => (
                        <MultiSelect
                          options={[
                            { label: "All Proxy Models", value: "all-proxy-models" },
                            { label: "No Default Models", value: "no-default-models" },
                            ...userModels.map((model) => ({ label: getModelDisplayName(model), value: model })),
                          ]}
                          value={value ?? []}
                          onValueChange={onChange}
                          placeholder="Select models"
                        />
                      )}
                    </FormField>
                  </CollapsibleContent>
                </Collapsible>
              </FieldGroup>

              <div className="mt-4 text-right">
                <Button type="submit">
                  <UserPlus />
                  Invite User
                </Button>
              </div>
            </form>
          </TooltipProvider>
        </DialogContent>
      </Dialog>
      {apiuser && (
        <OnboardingModal
          isInvitationLinkModalVisible={isInvitationLinkModalVisible}
          setIsInvitationLinkModalVisible={setIsInvitationLinkModalVisible}
          baseUrl={baseUrl || ""}
          invitationLinkData={invitationLinkData}
        />
      )}
    </>
  );
};
