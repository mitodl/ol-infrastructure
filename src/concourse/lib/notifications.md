
# Sending Slack Notifications from Concourse Jobs.

## Slack API Setup

1. Navigate to api.slack.com and login with touchstone.
2. Find the app named 'concourse-notifications'. You will need to be a collaborator on this app so talk to Mike or Tobias if you're not on the list.
3. Under 'Basic Information' for the app expand 'Add features and functionality` and then select 'Incoming Web Hooks`.
4. Then you can select 'Add New Webhook to Workspace'. Specify the channel you want it to send notification to. Each channel needs its own webhook.
   - **Important:** Don't commit the webhook URL to git. There is no authentication besides simply knowing the URL, so if it were public anyone could send us notifications in slack. Treat the webhook URL like a secret and put it in Vault.
