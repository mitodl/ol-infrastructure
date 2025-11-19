# B2B Partners Storage - Partner Account Setup Guide

## Overview

This document explains how partner AWS accounts can access their data in the MIT Open Learning B2B Partners Storage S3 bucket.

**Important**: You need access configured in **BOTH** AWS accounts:
1. ✅ **MIT's account** - Already configured (bucket policy grants your account access)
2. ⚠️ **Your account** - You must create IAM policies (instructions below)

## Quick Start

MIT has granted your AWS account access to a specific prefix in our S3 bucket. To use this access:

1. Create an IAM policy in your AWS account (copy-paste from examples below)
2. Attach the policy to your IAM users or roles
3. Test access using AWS CLI or SDK

## Required Configuration in Your AWS Account

### Step 1: Create IAM Policy

**Option A: Using AWS Console (Recommended for most users)**

1. Sign in to your AWS account
2. Go to **IAM** → **Policies** → **Create policy**
3. Click the **JSON** tab
4. **Copy and paste one of the policies below** (replace the placeholders first)
5. Click **Next: Tags** (optional)
6. Click **Next: Review**
7. Name the policy: `MIT-B2B-Storage-Access`
8. Click **Create policy**

**Option B: Using AWS CLI**

1. Save the policy JSON to a file (e.g., `mit-storage-policy.json`)
2. Run:
```bash
aws iam create-policy \
    --policy-name MIT-B2B-Storage-Access \
    --policy-document file://mit-storage-policy.json
```

### Step 2: Attach Policy to Users/Roles

**Using AWS Console:**
1. Go to **IAM** → **Users** (or **Roles**)
2. Select the user/role that needs access
3. Click **Add permissions** → **Attach policies directly**
4. Search for `MIT-B2B-Storage-Access`
5. Check the box and click **Add permissions**

**Using AWS CLI:**
```bash
# For a user
aws iam attach-user-policy \
    --user-name YOUR_USER_NAME \
    --policy-arn arn:aws:iam::YOUR_ACCOUNT_ID:policy/MIT-B2B-Storage-Access

# For a role
aws iam attach-role-policy \
    --role-name YOUR_ROLE_NAME \
    --policy-arn arn:aws:iam::YOUR_ACCOUNT_ID:policy/MIT-B2B-Storage-Access
```

## IAM Policy Examples

### Example 1: CI Environment (Most Common for Testing)

**Your MIT-provided information:**
- Environment: `ci`
- Bucket name: `ol-b2b-partners-storage-ci`
- Your prefix/username: `b2btestpartner1`
- Your AWS account ID: `713545616749`

**IAM Policy (copy this into AWS Console → IAM → Policies → Create Policy → JSON tab):**

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ListMITBucket",
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": "arn:aws:s3:::ol-b2b-partners-storage-ci",
            "Condition": {
                "StringLike": {
                    "s3:prefix": [
                        "b2btestpartner1/*",
                        "b2btestpartner1"
                    ]
                }
            }
        },
        {
            "Sid": "ReadMITBucketObjects",
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:GetObjectVersion",
                "s3:GetObjectTagging",
                "s3:GetObjectVersionTagging"
            ],
            "Resource": "arn:aws:s3:::ol-b2b-partners-storage-ci/b2btestpartner1/*"
        }
    ]
}
```

### Example 2: Production Environment

**Your MIT-provided information:**
- Environment: `production`
- Bucket name: `ol-b2b-partners-storage-production`
- Your prefix/username: `partner_org_name`
- Your AWS account ID: `123456789012`

**IAM Policy (copy this into AWS Console → IAM → Policies → Create Policy → JSON tab):**

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ListMITBucket",
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": "arn:aws:s3:::ol-b2b-partners-storage-production",
            "Condition": {
                "StringLike": {
                    "s3:prefix": [
                        "partner_org_name/*",
                        "partner_org_name"
                    ]
                }
            }
        },
        {
            "Sid": "ReadMITBucketObjects",
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:GetObjectVersion",
                "s3:GetObjectTagging",
                "s3:GetObjectVersionTagging"
            ],
            "Resource": "arn:aws:s3:::ol-b2b-partners-storage-production/partner_org_name/*"
        }
    ]
}
```

### Example 3: QA Environment

**Your MIT-provided information:**
- Environment: `qa`
- Bucket name: `ol-b2b-partners-storage-qa`
- Your prefix/username: `yourcompany`
- Your AWS account ID: `987654321098`

**IAM Policy (copy this into AWS Console → IAM → Policies → Create Policy → JSON tab):**

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ListMITBucket",
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": "arn:aws:s3:::ol-b2b-partners-storage-qa",
            "Condition": {
                "StringLike": {
                    "s3:prefix": [
                        "yourcompany/*",
                        "yourcompany"
                    ]
                }
            }
        },
        {
            "Sid": "ReadMITBucketObjects",
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:GetObjectVersion",
                "s3:GetObjectTagging",
                "s3:GetObjectVersionTagging"
            ],
            "Resource": "arn:aws:s3:::ol-b2b-partners-storage-qa/yourcompany/*"
        }
    ]
}
```

### Customizing the Policy for Your Organization

**To customize for your specific setup, replace these values:**

1. **Bucket name** (`ol-b2b-partners-storage-{ENV}`):
   - Replace `{ENV}` with: `ci`, `qa`, or `production`
   - Example: `ol-b2b-partners-storage-ci`

2. **Your prefix** (appears 4 times in the policy):
   - Replace `b2btestpartner1` with your assigned username
   - Must match what MIT provided to you
   - Example: If MIT said "your prefix is acme_corp", use `acme_corp`

**Where to find your values:**
- MIT will provide these in your onboarding email
- Environment: Usually `production` for real data, `ci` or `qa` for testing
- Prefix/Username: Your organization identifier (e.g., company name)

## Testing Your Access

### Step 3: Verify Setup

After creating and attaching the IAM policy, test your access:

**Using AWS CLI:**

```bash
# 1. Verify you're using the correct AWS account
aws sts get-caller-identity
# Should show your partner AWS account ID

# 2. List files in your prefix
aws s3 ls s3://ol-b2b-partners-storage-ci/b2btestpartner1/
# Replace 'ci' and 'b2btestpartner1' with your environment and prefix

# 3. Download a specific file
aws s3 cp s3://ol-b2b-partners-storage-ci/b2btestpartner1/example.txt ./
# Replace with an actual filename MIT has placed in your prefix

# 4. List files using S3 API (more detailed output)
aws s3api list-objects-v2 \
    --bucket ol-b2b-partners-storage-ci \
    --prefix b2btestpartner1/
```

**Using Python (boto3):**

```python
import boto3

# Initialize S3 client
s3 = boto3.client('s3')

# Your configuration (provided by MIT)
bucket_name = 'ol-b2b-partners-storage-ci'
prefix = 'b2btestpartner1/'

# List objects in your prefix
response = s3.list_objects_v2(
    Bucket=bucket_name,
    Prefix=prefix
)

if 'Contents' in response:
    print(f"Files in {prefix}:")
    for obj in response['Contents']:
        print(f"  - {obj['Key']} ({obj['Size']} bytes)")
else:
    print(f"No files found in {prefix}")

# Download a file
s3.download_file(
    bucket_name,
    f'{prefix}example.txt',
    'local-example.txt'
)
print("File downloaded successfully!")
```

**Using AWS SDK for Java:**

```java
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.*;
import java.util.List;

public class MITStorageAccess {
    public static void main(String[] args) {
        S3Client s3 = S3Client.create();

        String bucketName = "ol-b2b-partners-storage-ci";
        String prefix = "b2btestpartner1/";

        // List objects
        ListObjectsV2Request listRequest = ListObjectsV2Request.builder()
            .bucket(bucketName)
            .prefix(prefix)
            .build();

        ListObjectsV2Response listResponse = s3.listObjectsV2(listRequest);
        List<S3Object> objects = listResponse.contents();

        System.out.println("Files in " + prefix + ":");
        for (S3Object obj : objects) {
            System.out.println("  - " + obj.key() + " (" + obj.size() + " bytes)");
        }

        // Download file
        GetObjectRequest getRequest = GetObjectRequest.builder()
            .bucket(bucketName)
            .key(prefix + "example.txt")
            .build();

        s3.getObject(getRequest,
            java.nio.file.Paths.get("local-example.txt"));
        System.out.println("File downloaded successfully!");
    }
}
```

## Troubleshooting

### Error: "Access Denied" when listing bucket

**Symptom:**
```
An error occurred (AccessDenied) when calling the ListObjectsV2 operation:
Access Denied
```

**Common Causes & Solutions:**

1. **IAM policy not created in your account**
   - Solution: Follow Step 1 above to create the IAM policy
   - Verify: Check IAM → Policies in your AWS account for `MIT-B2B-Storage-Access`

2. **IAM policy not attached to your user/role**
   - Solution: Follow Step 2 above to attach the policy
   - Verify: Check your user → Permissions tab for the policy name

3. **Wrong AWS account**
   - Your credentials might be for a different AWS account
   - Verify by running: `aws sts get-caller-identity`
   - Should show the partner account ID MIT provided

4. **Wrong prefix/username**
   - You're trying to access a different prefix than assigned
   - Verify the correct prefix with MIT
   - Your policy must match the prefix exactly

5. **Typo in bucket name**
   - Verify bucket name: `ol-b2b-partners-storage-{environment}`
   - Environment is: `ci`, `qa`, or `production`
   - Check your IAM policy JSON for typos

### Error: "Access Denied" even with correct IAM policy

**Possible causes:**

1. **MIT hasn't added your account to the bucket policy yet**
   - Contact MIT to verify your AWS account ID is configured
   - They will verify: `aws s3api get-bucket-policy --bucket ol-b2b-partners-storage-ci`

2. **Wrong AWS account ID provided to MIT**
   - Run: `aws sts get-caller-identity --query Account --output text`
   - Confirm this matches what you gave MIT
   - If different, provide MIT with the correct account ID

3. **Policy was just created/attached**
   - IAM changes can take a few seconds to propagate
   - Wait 30-60 seconds and try again

### Error: "NoSuchBucket"

**Symptom:**
```
An error occurred (NoSuchBucket) when calling the ListObjectsV2 operation:
The specified bucket does not exist
```

**Solution:**
- Check the bucket name spelling
- Verify the environment name (ci/qa/production)
- Bucket names are: `ol-b2b-partners-storage-{environment}`

### Error: "NoSuchKey" when downloading a file

**Symptom:**
```
An error occurred (404) when calling the HeadObject operation: Not Found
```

**Solution:**
- The file doesn't exist in your prefix
- List available files first: `aws s3 ls s3://bucket-name/your-prefix/`
- Verify the file path includes your prefix
- Contact MIT if expected files are missing

### How to Verify Your Setup

Run these commands to diagnose issues:

```bash
# 1. Confirm your AWS account ID
aws sts get-caller-identity

# 2. Check if IAM policy exists in your account
aws iam list-policies --scope Local --query "Policies[?PolicyName=='MIT-B2B-Storage-Access']"

# 3. Check if policy is attached to your user (replace YOUR_USER)
aws iam list-attached-user-policies --user-name YOUR_USER

# 4. View the policy document to check for typos
aws iam get-policy-version \
    --policy-arn arn:aws:iam::YOUR_ACCOUNT_ID:policy/MIT-B2B-Storage-Access \
    --version-id v1 \
    --query 'PolicyVersion.Document'

# 5. Test bucket access with detailed error output
aws s3 ls s3://ol-b2b-partners-storage-ci/your-prefix/ --debug
```

## Detailed Setup Instructions

### Visual Guide: Creating IAM Policy in AWS Console

**Step-by-step with screenshots:**

1. **Navigate to IAM**
   - Sign in to AWS Console
   - Search for "IAM" in the top search bar
   - Click "IAM" (Identity and Access Management)

2. **Create New Policy**
   - Click "Policies" in the left sidebar
   - Click "Create policy" button (blue button, top right)

3. **Switch to JSON Editor**
   - You'll see a visual editor by default
   - Click the "JSON" tab (next to "Visual")
   - Delete all the existing JSON

4. **Paste Policy JSON**
   - Copy one of the example policies above
   - Customize it with your bucket name and prefix
   - Paste into the JSON editor
   - Click "Next"

5. **Review and Name**
   - Policy name: `MIT-B2B-Storage-Access`
   - Description (optional): "Access to MIT B2B Partners Storage bucket"
   - Click "Create policy"

6. **Attach to User**
   - Click "Users" in the left sidebar
   - Click on the username that needs access
   - Click "Add permissions" button
   - Select "Attach policies directly"
   - Search for "MIT-B2B-Storage-Access"
   - Check the box next to it
   - Click "Add permissions"

### Alternative: Using IAM Groups (Recommended for Multiple Users)

If multiple people in your organization need access:

1. **Create an IAM Group:**
   - IAM → Groups → Create New Group
   - Group name: `MIT-Storage-Users`
   - Attach the `MIT-B2B-Storage-Access` policy to the group

2. **Add Users to Group:**
   - IAM → Users → Select user
   - Groups tab → Add user to groups
   - Select `MIT-Storage-Users`

This way, you manage access by adding/removing users from the group.

## Security & Access Control

### What You CAN Do
- ✅ List files within your assigned prefix
- ✅ Download/read files within your assigned prefix
- ✅ Read file metadata and tags within your prefix
- ✅ Access historical versions of files (if versioning is enabled)

### What You CANNOT Do
- ❌ List files in other partners' prefixes
- ❌ Download/read files from other partners' prefixes
- ❌ List the bucket root without specifying your prefix
- ❌ Upload, modify, or delete files (read-only access)
- ❌ Change bucket configuration or policies
- ❌ See bucket-level information (owner, creation date, etc.)

### Access Boundaries

Your access is strictly limited to your prefix. Examples:

**Your prefix: `partner1`**

✅ **Allowed:**
- `s3://ol-b2b-partners-storage-ci/partner1/data.csv`
- `s3://ol-b2b-partners-storage-ci/partner1/2024/report.pdf`
- `s3://ol-b2b-partners-storage-ci/partner1/subfolder/anything.txt`

❌ **Blocked:**
- `s3://ol-b2b-partners-storage-ci/` (bucket root)
- `s3://ol-b2b-partners-storage-ci/partner2/data.csv` (other prefix)
- `s3://ol-b2b-partners-storage-ci/shared/file.txt` (outside your prefix)

### Best Practices

1. **Use IAM Roles for Applications**
   - Create an IAM role (not user) for applications/scripts
   - Attach the MIT storage policy to the role
   - Use temporary credentials via STS

2. **Principle of Least Privilege**
   - Only give access to users/systems that need it
   - Use IAM groups to manage access for multiple users
   - Regularly audit who has access

3. **Monitor Access**
   - Enable CloudTrail in your account to log S3 API calls
   - Set up alerts for unexpected access patterns
   - Review access logs periodically

4. **Credential Security**
   - Never commit AWS credentials to code repositories
   - Use AWS IAM roles for EC2/Lambda instead of access keys
   - Rotate access keys regularly if using them

## Getting Help

### Before Contacting MIT

1. Verify your IAM policy is created and attached
2. Run diagnostic commands from "Troubleshooting" section
3. Check your AWS account ID matches what you provided MIT
4. Verify you're using the correct bucket name and prefix

### When Contacting MIT Support

Include this information:
- **Your AWS Account ID**: (from `aws sts get-caller-identity`)
- **Your assigned prefix/username**: (provided by MIT)
- **Environment**: (ci, qa, or production)
- **Exact error message**: (copy-paste the full error)
- **Command you ran**: (e.g., `aws s3 ls s3://...`)
- **Output of**: `aws sts get-caller-identity`

### MIT Contact Information
- Email: [Contact details from MIT]
- Include "B2B Partners Storage" in the subject line

## Appendix

### For DevOps Teams: Infrastructure as Code Examples

If you manage infrastructure with code, here are examples for common tools:

#### Terraform

```hcl
# variables.tf
variable "mit_bucket_name" {
  description = "MIT B2B Partners Storage bucket name"
  type        = string
  default     = "ol-b2b-partners-storage-production"
}

variable "mit_prefix" {
  description = "Your assigned prefix in MIT bucket"
  type        = string
  # Set this via terraform.tfvars or environment variable
}

# iam.tf
resource "aws_iam_policy" "mit_storage" {
  name        = "MIT-B2B-Storage-Access"
  description = "Access to MIT B2B Partners Storage bucket"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ListMITBucket"
        Effect = "Allow"
        Action = "s3:ListBucket"
        Resource = "arn:aws:s3:::${var.mit_bucket_name}"
        Condition = {
          StringLike = {
            "s3:prefix" = [
              "${var.mit_prefix}/*",
              var.mit_prefix
            ]
          }
        }
      },
      {
        Sid    = "ReadMITBucketObjects"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion",
          "s3:GetObjectTagging",
          "s3:GetObjectVersionTagging"
        ]
        Resource = "arn:aws:s3:::${var.mit_bucket_name}/${var.mit_prefix}/*"
      }
    ]
  })
}

# Attach to a user
resource "aws_iam_user_policy_attachment" "mit_storage_user" {
  user       = aws_iam_user.data_reader.name
  policy_arn = aws_iam_policy.mit_storage.arn
}

# Or attach to a role
resource "aws_iam_role_policy_attachment" "mit_storage_role" {
  role       = aws_iam_role.app_role.name
  policy_arn = aws_iam_policy.mit_storage.arn
}
```

#### AWS CloudFormation

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: 'IAM policy for MIT B2B Partners Storage access'

Parameters:
  MITBucketName:
    Type: String
    Default: ol-b2b-partners-storage-production
    Description: MIT bucket name

  MITPrefix:
    Type: String
    Description: Your assigned prefix in MIT bucket

Resources:
  MITStoragePolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      ManagedPolicyName: MIT-B2B-Storage-Access
      Description: Access to MIT B2B Partners Storage bucket
      PolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Sid: ListMITBucket
            Effect: Allow
            Action: s3:ListBucket
            Resource: !Sub 'arn:aws:s3:::${MITBucketName}'
            Condition:
              StringLike:
                s3:prefix:
                  - !Sub '${MITPrefix}/*'
                  - !Ref MITPrefix
          - Sid: ReadMITBucketObjects
            Effect: Allow
            Action:
              - s3:GetObject
              - s3:GetObjectVersion
              - s3:GetObjectTagging
              - s3:GetObjectVersionTagging
            Resource: !Sub 'arn:aws:s3:::${MITBucketName}/${MITPrefix}/*'

Outputs:
  PolicyArn:
    Description: ARN of the created policy
    Value: !Ref MITStoragePolicy
    Export:
      Name: MIT-Storage-Policy-ARN
```

#### AWS CDK (Python)

```python
from aws_cdk import (
    aws_iam as iam,
    Stack,
    CfnParameter,
)
from constructs import Construct

class MITStorageAccessStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # Parameters
        bucket_name = CfnParameter(
            self, "BucketName",
            type="String",
            default="ol-b2b-partners-storage-production",
            description="MIT bucket name"
        )

        prefix = CfnParameter(
            self, "Prefix",
            type="String",
            description="Your assigned prefix"
        )

        # Create IAM policy
        policy = iam.ManagedPolicy(
            self, "MITStoragePolicy",
            managed_policy_name="MIT-B2B-Storage-Access",
            description="Access to MIT B2B Partners Storage bucket",
            statements=[
                iam.PolicyStatement(
                    sid="ListMITBucket",
                    effect=iam.Effect.ALLOW,
                    actions=["s3:ListBucket"],
                    resources=[f"arn:aws:s3:::{bucket_name.value_as_string}"],
                    conditions={
                        "StringLike": {
                            "s3:prefix": [
                                f"{prefix.value_as_string}/*",
                                prefix.value_as_string,
                            ]
                        }
                    }
                ),
                iam.PolicyStatement(
                    sid="ReadMITBucketObjects",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "s3:GetObject",
                        "s3:GetObjectVersion",
                        "s3:GetObjectTagging",
                        "s3:GetObjectVersionTagging",
                    ],
                    resources=[
                        f"arn:aws:s3:::{bucket_name.value_as_string}/{prefix.value_as_string}/*"
                    ]
                )
            ]
        )
```

### AWS CLI Reference

**Complete setup via CLI:**

```bash
#!/bin/bash
# Setup script for MIT B2B Storage access

# Configuration
BUCKET_NAME="ol-b2b-partners-storage-production"
PREFIX="your_prefix"
POLICY_NAME="MIT-B2B-Storage-Access"
USER_NAME="data-reader"

# Create policy document
cat > /tmp/mit-storage-policy.json <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ListMITBucket",
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": "arn:aws:s3:::${BUCKET_NAME}",
            "Condition": {
                "StringLike": {
                    "s3:prefix": [
                        "${PREFIX}/*",
                        "${PREFIX}"
                    ]
                }
            }
        },
        {
            "Sid": "ReadMITBucketObjects",
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:GetObjectVersion",
                "s3:GetObjectTagging",
                "s3:GetObjectVersionTagging"
            ],
            "Resource": "arn:aws:s3:::${BUCKET_NAME}/${PREFIX}/*"
        }
    ]
}
EOF

# Create IAM policy
POLICY_ARN=$(aws iam create-policy \
    --policy-name $POLICY_NAME \
    --policy-document file:///tmp/mit-storage-policy.json \
    --query 'Policy.Arn' \
    --output text)

echo "Created policy: $POLICY_ARN"

# Attach to user
aws iam attach-user-policy \
    --user-name $USER_NAME \
    --policy-arn $POLICY_ARN

echo "Attached policy to user: $USER_NAME"

# Clean up
rm /tmp/mit-storage-policy.json

# Test access
echo "Testing access..."
aws s3 ls s3://${BUCKET_NAME}/${PREFIX}/
```

### FAQ

**Q: Do I need to pay for accessing MIT's S3 bucket?**
A: Data transfer charges may apply in your AWS account. Contact MIT for details on cost sharing.

**Q: Can I write data to the bucket?**
A: No, partners have read-only access. Contact MIT if you need to send data back.

**Q: How often is data updated?**
A: Contact MIT for information about data refresh schedules.

**Q: Can I automate data downloads?**
A: Yes, use AWS SDK or CLI in your scripts. Consider using IAM roles for automation.

**Q: What if I need access to multiple prefixes?**
A: Contact MIT. They can configure additional prefixes or update your existing access.

**Q: Is data encrypted?**
A: Yes, S3 provides encryption at rest. Data in transit uses HTTPS/TLS.

**Q: Can I access this from on-premises systems?**
A: Yes, as long as your systems can reach AWS S3 endpoints and have valid AWS credentials.

---

**Document Version**: 1.0
**Last Updated**: 2025-11-14
**Maintained By**: MIT Open Learning Infrastructure Team
