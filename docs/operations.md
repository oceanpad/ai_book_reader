# Private reader operations

This is a private manual-reading deployment, not the complete release described in the [implementation plan](../AI_Book_Reader_Implementation_Plan.md). Keep public registration disabled. The current app has no saved book library or automatic capture queue.

## Useful locations

- Public URL/resource identifiers: ignored `.local/deployment/outputs.json`.
- Selected account/region/origin: ignored `.local/deployment/target.json`.
- First-login password: ignored `.local/deployment/first-login.json`, mode `0600`; remove only after the initial password change.
- Deployment/check logs and isolated smoke result: ignored `.local/deployment/`. Do not publish credentials, tokens, signed media URLs, or copied book content in reports.

## Alarms and costs

CloudWatch alarms cover API/worker/dispatcher/maintenance Lambda errors, SQS oldest-message age, and the processing/failure dead-letter queue. Logs retain 14 days; request logs contain route/status identifiers, not payloads or authorization values. Application failures are exposed in capture status; unexpected infrastructure failures are redelivered rather than reported as success.

Confirm the SNS subscription sent to the supplied adult/alert email. Confirmed subscription status is required for delivery; creating the topic is not proof that notifications will arrive. Check the topic ARN from deployment outputs in the SNS console.

**Operational alarms are not financial alerts.** No account-wide budget or monetary spending cap is configured by this private deployment. Configure an application-appropriate AWS Budget and provider spending limits before broader use. Budgets are delayed alerts, not hard stops. Durable daily attempt quotas include retries/chunks, but cannot guarantee a monetary maximum. Four-orientation OCR uses more image tokens than one view. Model calls can be billed again after a crash before checkpointing.

The account has a total Lambda concurrency limit of 10. New page jobs use one FIFO message group, so they run one at a time; there is no reserved Lambda capacity. The prior standard queue is retained to drain legacy deliveries. Other applications in the account can cause throttling. If sustained, review account/service quotas and usage instead of increasing worker parallelism blindly.

## Credentials and account recovery

Provider keys are stored in one Secrets Manager secret referenced by `ProviderSecretArn`. Only the worker role has `GetSecretValue`; API/browser/dispatcher/maintenance do not. To rotate, update the ignored local provider configuration and rerun the reviewed deployment with `--execute`; the bootstrap SDK overwrites the secret without putting values in arguments or outputs. Workers read it for each SQS invocation, so no secret cache flush is required. Revoke the old provider key after a synthetic read confirms the new key works.

For the adult account, select **Account help** on the inline sign-in form to open Cognito managed login's password recovery. If first login was never completed and the local temporary password is lost, use an explicitly authorized Cognito administrator recovery/reset; redeploy does **not** silently overwrite an existing password. Do not paste passwords or tokens into chat, issues, terminal logs, or committed files.

Access tokens expire; the app returns to Sign In rather than continuing paid requests with expired credentials. Sign Out clears tab credentials and ends managed login. Reloading loses the current capture/run reference; signed-in history is available after reload; unfinished work expires automatically.

## Job recovery

- The API must commit `QUEUED` before dispatch. Stream failures are retried and alarmed. Maintenance re-enqueues old queued jobs, covering stranded dispatches.
- Duplicate SQS deliveries do not acquire an active lease. Maintenance fences/requeues expired workers using a new attempt number. A stale token/attempt cannot checkpoint over a recovered or cancelled job.
- Retry Audio resumes recognized text and completed chunks. Automatic stage retries are bounded at three; explicit retries still consume durable daily quota.
- A missing/expired image requires a fresh capture. Unreadable/non-book outcomes do not trigger silent repeated paid work.
- Do not manually rewrite job state/lease tokens in DynamoDB or blindly redrive DLQs. Inspect job IDs, attempt, stage, soft expiry, quota and alarms first. Invoke the maintenance Lambda from the console to run the existing repair path, or use the UI's explicit retry for a recoverable failed page.

SQS visibility is 60 minutes, six times the ten-minute worker timeout. Provider operations have a wall-time bound, and the worker renews its two-minute fenced lease at checkpoints. The scheduled maintenance repair interval is five minutes, so infrastructure recovery is not instantaneous.

Homework checks use the same dispatcher and worker, with a separate FIFO group per account so they do not queue behind book narration. Homework has a separate daily counter with the same configured limit as book captures. Maintenance re-dispatches pending checks older than five minutes and marks checks still pending after 20 minutes as failed. Worksheet photos are deleted after processing, with the existing `incoming/` one-day lifecycle as a fallback. Results stay in account history until deleted; before removing an account, delete its homework history too.

## Discard, expiry and deletion

Publishing `READY` releases the cloud run’s active-job slot in the same transaction, and retains the completed recording until explicit deletion. The next capture in that run does not discard playable history. A non-ready job still occupies the slot until discarded or expired.

Discard marks the selected job `CANCELLED` and releases the slot only if that job still holds it. API/media requests then return 404. In-flight work may finish a provider request but cannot publish a new accepted result. Private object cleanup repeats for 20 minutes, exceeding API/worker lifetimes, while cancellation tombstones remain 24 hours. This covers late uploads and media writes. Saved history media is exempt from temporary-object expiry.

Unfinished work expires after one hour. New READY full-size page images, thumbnails, narration and audio are retained, so storage costs can grow. Before removing a Cognito user, delete their saved captures and verify media cleanup; deleting the identity alone does not erase saved history. DynamoDB TTL removal of temporary records is asynchronous. Do not delete unrelated users' data.

## Redeploy and rollback

Run `npm run check`, review the exact-account CDK diff, then execute the deployment as documented in [deployment](deployment.md). Keep `.local/deployment/target.json`: it pins the literal CloudFront origin for Cognito callbacks and media CORS. Do not change the account/region in-place.

Frontend uploads retain older fingerprinted assets; the new index points to the new build. For an application rollback, restore a known-good source revision in a separate, clean worktree, validate/build/package it, and redeploy to the same target using the existing origin. Do not reset/revert someone else's dirty working tree. Review CloudFormation changes carefully: data table/media/user pool are retained, and the table has deletion protection. Never delete healthy data/auth stacks as a rollback shortcut.

The first creation attempt failed solely because AWS rejected reserved Lambda concurrency. Its new API stack reached `ROLLBACK_COMPLETE` and was removed before recreating the corrected stack; healthy data/auth resources were preserved. Some empty failure-attempt log groups may remain retained, with 14-day event retention. No existing unrelated stacks or account quota were modified.
