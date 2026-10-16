import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/lib/toast";
import { Copy, KeyRound, Plus } from "lucide-react";
import React, { useState } from "react";
import {
  CollectedKey,
  KeyRequest,
  KeyRequestStatus,
  keyRequestApproveCall,
  keyRequestCancelCall,
  keyRequestCollectCall,
  keyRequestCreateCall,
  keyRequestRejectCall,
  KeyTermsInput,
  NewKeyRequestInput,
  useKeyRequestAction,
  useKeyRequests,
} from "./keyRequests";

interface KeyRequestsTabProps {
  teamId: string;
  teamModels: string[];
  canReview: boolean;
}

interface TermsDraft {
  keyAlias: string;
  models: string[];
  maxBudget: string;
  duration: string;
  text: string;
}

type Dialogs =
  | { kind: "none" }
  | { kind: "request" }
  | { kind: "approve"; request: KeyRequest }
  | { kind: "reject"; request: KeyRequest }
  | { kind: "reveal"; collected: CollectedKey };

const STATUS_VARIANT: Record<KeyRequestStatus, "default" | "secondary" | "destructive" | "outline"> = {
  pending: "outline",
  approved: "default",
  rejected: "destructive",
  cancelled: "secondary",
};

const emptyDraft: TermsDraft = { keyAlias: "", models: [], maxBudget: "", duration: "", text: "" };

const draftFrom = (request: KeyRequest): TermsDraft => ({
  keyAlias: request.key_alias ?? "",
  models: request.models,
  maxBudget: request.max_budget === null || request.max_budget === undefined ? "" : String(request.max_budget),
  duration: request.duration ?? "",
  text: "",
});

export const termsFromDraft = (draft: TermsDraft): KeyTermsInput => ({
  key_alias: draft.keyAlias.trim() || null,
  models: draft.models,
  max_budget: draft.maxBudget.trim() === "" ? null : Number(draft.maxBudget),
  duration: draft.duration.trim() || null,
});

const formatBudget = (value: number | null | undefined) =>
  value === null || value === undefined ? "No limit" : `$${value}`;

function TermsFields({
  draft,
  onChange,
  modelChoices,
  textLabel,
}: {
  draft: TermsDraft;
  onChange: (draft: TermsDraft) => void;
  modelChoices: string[];
  textLabel: string;
}) {
  const toggleModel = (model: string, checked: boolean) =>
    onChange({ ...draft, models: checked ? [...draft.models, model] : draft.models.filter((m) => m !== model) });
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <Label htmlFor="key-request-alias">Key name</Label>
        <Input
          id="key-request-alias"
          value={draft.keyAlias}
          onChange={(e) => onChange({ ...draft, keyAlias: e.target.value })}
        />
      </div>
      {modelChoices.length > 0 && (
        <div className="flex flex-col gap-1">
          <Label>Models (none selected means all team models)</Label>
          <div className="flex flex-wrap gap-3">
            {modelChoices.map((model) => (
              <label key={model} className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={draft.models.includes(model)}
                  onCheckedChange={(checked) => toggleModel(model, checked === true)}
                  aria-label={model}
                />
                {model}
              </label>
            ))}
          </div>
        </div>
      )}
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor="key-request-budget">Max budget (USD)</Label>
          <Input
            id="key-request-budget"
            type="number"
            min={0}
            value={draft.maxBudget}
            onChange={(e) => onChange({ ...draft, maxBudget: e.target.value })}
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="key-request-duration">Expires after (e.g. 30d)</Label>
          <Input
            id="key-request-duration"
            value={draft.duration}
            onChange={(e) => onChange({ ...draft, duration: e.target.value })}
          />
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="key-request-text">{textLabel}</Label>
        <Textarea
          id="key-request-text"
          value={draft.text}
          onChange={(e) => onChange({ ...draft, text: e.target.value })}
        />
      </div>
    </div>
  );
}

export default function KeyRequestsTab({ teamId, teamModels, canReview }: KeyRequestsTabProps) {
  const { userId } = useAuthorized();
  const { data, isLoading, error } = useKeyRequests(teamId);
  const [dialog, setDialog] = useState<Dialogs>({ kind: "none" });
  const [draft, setDraft] = useState<TermsDraft>(emptyDraft);

  const create = useKeyRequestAction(teamId, (token, body: NewKeyRequestInput) => keyRequestCreateCall(token, body));
  const approve = useKeyRequestAction(
    teamId,
    (token, args: { id: string; body: KeyTermsInput & { note?: string | null } }) =>
      keyRequestApproveCall(token, args.id, args.body),
  );
  const reject = useKeyRequestAction(teamId, (token, args: { id: string; note: string | null }) =>
    keyRequestRejectCall(token, args.id, args.note),
  );
  const cancel = useKeyRequestAction(teamId, (token, id: string) => keyRequestCancelCall(token, id));
  const collect = useKeyRequestAction(teamId, (token, id: string) => keyRequestCollectCall(token, id));

  const close = () => setDialog({ kind: "none" });
  const open = (next: Dialogs, nextDraft: TermsDraft = emptyDraft) => {
    setDraft(nextDraft);
    setDialog(next);
  };

  const run = async (action: () => Promise<unknown>, success: string) => {
    try {
      await action();
      toast.success(success);
      close();
    } catch (err) {
      toast.fromError(err);
    }
  };

  const submit = () => {
    const note = draft.text.trim() || null;
    if (dialog.kind === "request") {
      return run(
        () => create.mutateAsync({ team_id: teamId, ...termsFromDraft(draft), reason: note }),
        "Key requested. A team admin will review it",
      );
    }
    if (dialog.kind === "approve") {
      return run(
        () => approve.mutateAsync({ id: dialog.request.request_id, body: { ...termsFromDraft(draft), note } }),
        "Key request approved. The requester can now collect the key",
      );
    }
    if (dialog.kind === "reject") {
      return run(() => reject.mutateAsync({ id: dialog.request.request_id, note }), "Key request rejected");
    }
  };

  const reveal = async (request: KeyRequest) => {
    try {
      const collected = await collect.mutateAsync(request.request_id);
      setDialog({ kind: "reveal", collected });
    } catch (err) {
      toast.fromError(err);
    }
  };

  const requests = data ?? [];
  const busy = create.isPending || approve.isPending || reject.isPending;

  const listMessage = (): React.ReactNode => {
    if (isLoading) return <span className="text-muted-foreground">Loading key requests...</span>;
    if (error) {
      return (
        <span className="text-destructive">
          {error instanceof Error ? error.message : "Failed to load key requests"}
        </span>
      );
    }
    if (requests.length === 0) return <span className="text-muted-foreground">No key requests yet</span>;
    return null;
  };

  const dialogBody = (): React.ReactNode => {
    if (dialog.kind === "none") return null;
    if (dialog.kind === "reveal") {
      const { key } = dialog.collected;
      return (
        <>
          <DialogHeader>
            <DialogTitle>Your new key</DialogTitle>
            <DialogDescription>Copy it now. It will not be shown again</DialogDescription>
          </DialogHeader>
          <div className="flex items-center gap-2">
            <code className="flex-1 break-all rounded bg-muted p-2 text-sm">{key}</code>
            <Button
              variant="outline"
              aria-label="Copy key"
              onClick={() => navigator.clipboard.writeText(key).then(() => toast.success("Copied"))}
            >
              <Copy />
            </Button>
          </div>
          <DialogFooter>
            <Button onClick={close}>Done</Button>
          </DialogFooter>
        </>
      );
    }
    if (dialog.kind === "reject") {
      return (
        <>
          <DialogHeader>
            <DialogTitle>Reject key request</DialogTitle>
            <DialogDescription>The requester sees your note</DialogDescription>
          </DialogHeader>
          <Textarea
            aria-label="Note"
            value={draft.text}
            onChange={(e) => setDraft({ ...draft, text: e.target.value })}
          />
          <DialogFooter>
            <Button variant="outline" onClick={close}>
              Back
            </Button>
            <Button variant="destructive" disabled={busy} onClick={submit}>
              Reject
            </Button>
          </DialogFooter>
        </>
      );
    }
    const requesting = dialog.kind === "request";
    return (
      <>
        <DialogHeader>
          <DialogTitle>{requesting ? "Request a key" : "Approve key request"}</DialogTitle>
          <DialogDescription>
            {requesting
              ? "A team admin reviews the request before the key is created"
              : "You can narrow the models and lower the budget before approving"}
          </DialogDescription>
        </DialogHeader>
        <TermsFields
          draft={draft}
          onChange={setDraft}
          modelChoices={!requesting && dialog.request.models.length ? dialog.request.models : teamModels}
          textLabel={requesting ? "Reason" : "Note to the requester"}
        />
        <DialogFooter>
          <Button variant="outline" onClick={close}>
            Back
          </Button>
          <Button disabled={busy} onClick={submit}>
            {requesting ? "Send request" : "Approve and create key"}
          </Button>
        </DialogFooter>
      </>
    );
  };

  return (
    <div className="flex w-full flex-col gap-4">
      <div className="flex items-center justify-between">
        <p className="text-muted-foreground">
          {canReview
            ? "Members ask for keys here. Approving creates the key for them; you can lower the budget or models first"
            : "Ask a team admin for a key. Once approved you can view it here once"}
        </p>
        <Button onClick={() => open({ kind: "request" })}>
          <Plus />
          Request key
        </Button>
      </div>

      <Card>
        <CardContent>
          {listMessage() ?? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Requested by</TableHead>
                  <TableHead>Key name</TableHead>
                  <TableHead>Models</TableHead>
                  <TableHead>Budget</TableHead>
                  <TableHead>Expires after</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Notes</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {requests.map((request) => {
                  const mine = request.user_id === userId;
                  const pending = request.status === "pending";
                  return (
                    <TableRow key={request.request_id}>
                      <TableCell>{request.user_id}</TableCell>
                      <TableCell>{request.key_alias || "-"}</TableCell>
                      <TableCell>{request.models.length ? request.models.join(", ") : "All team models"}</TableCell>
                      <TableCell>{formatBudget(request.max_budget)}</TableCell>
                      <TableCell>{request.duration || "Never"}</TableCell>
                      <TableCell>
                        <Badge variant={STATUS_VARIANT[request.status]}>{request.status}</Badge>
                      </TableCell>
                      <TableCell className="max-w-64 whitespace-normal text-sm">
                        {request.reason && <div>{request.reason}</div>}
                        {request.review_note && (
                          <div className="text-muted-foreground">
                            {request.reviewed_by}: {request.review_note}
                          </div>
                        )}
                      </TableCell>
                      <TableCell className="flex gap-2">
                        {canReview && pending && (
                          <>
                            <Button size="sm" onClick={() => open({ kind: "approve", request }, draftFrom(request))}>
                              Approve
                            </Button>
                            <Button size="sm" variant="outline" onClick={() => open({ kind: "reject", request })}>
                              Reject
                            </Button>
                          </>
                        )}
                        {mine && pending && (
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => run(() => cancel.mutateAsync(request.request_id), "Key request cancelled")}
                          >
                            Cancel
                          </Button>
                        )}
                        {mine && request.status === "approved" && request.key_ready && (
                          <Button size="sm" variant="outline" onClick={() => reveal(request)}>
                            <KeyRound />
                            View key
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Dialog open={dialog.kind !== "none"} onOpenChange={(isOpen) => !isOpen && close()}>
        <DialogContent>{dialogBody()}</DialogContent>
      </Dialog>
    </div>
  );
}
