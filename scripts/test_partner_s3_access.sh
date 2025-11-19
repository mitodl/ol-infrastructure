#!/bin/bash
# Test script to validate partner S3 access
# Usage: ./test_partner_s3_access.sh <bucket-name> <prefix>
#
# This script should be run FROM THE PARTNER AWS ACCOUNT to test access

set -e

BUCKET_NAME="${1:-ol-uai-partners-storage-ci}"
PREFIX="${2:-uaitestpartner1}"

echo "========================================="
echo "UAI Partners Storage Access Test"
echo "========================================="
echo ""

# Get current AWS identity
echo "Current AWS Identity:"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --no-cli-pager)
USER_ARN=$(aws sts get-caller-identity --query Arn --output text --no-cli-pager)
echo "  Account: $ACCOUNT_ID"
echo "  ARN: $USER_ARN"
echo ""

# Create test file in MIT account (only works if running from MIT account)
if [[ "$ACCOUNT_ID" == "610119931565" ]]; then
    echo "⚠️  You are running from the MIT bucket owner account"
    echo "   Creating test file for partner access testing..."
    echo "test-$(date +%s)" | aws s3 cp - "s3://${BUCKET_NAME}/${PREFIX}/test-access.txt" --no-cli-pager
    echo "   ✓ Test file created"
    echo ""
    echo "   To test partner access, run this script from the partner AWS account (713545616749)"
    echo ""
fi

# Test 1: List bucket root (should fail for partners)
echo "Test 1: List bucket root (partners should see Access Denied)"
echo "  Command: aws s3 ls s3://${BUCKET_NAME}/"
if aws s3 ls "s3://${BUCKET_NAME}/" --no-cli-pager 2>&1 | head -3; then
    echo "  ✓ List succeeded (you are bucket owner)"
else
    EXIT_CODE=$?
    if [[ $EXIT_CODE -eq 254 ]] || [[ $EXIT_CODE -eq 1 ]]; then
        echo "  ✓ Access Denied as expected (you are partner)"
    else
        echo "  ✗ Unexpected error (exit code: $EXIT_CODE)"
    fi
fi
echo ""

# Test 2: List partner prefix
echo "Test 2: List partner prefix (should succeed for both)"
echo "  Command: aws s3 ls s3://${BUCKET_NAME}/${PREFIX}/"
if aws s3 ls "s3://${BUCKET_NAME}/${PREFIX}/" --no-cli-pager 2>&1; then
    echo "  ✓ Successfully listed prefix"
else
    EXIT_CODE=$?
    echo "  ✗ FAILED to list prefix (exit code: $EXIT_CODE)"
    echo ""
    echo "Troubleshooting:"
    echo "  1. If you are partner account (713545616749), verify you have IAM policy attached"
    echo "  2. If you are MIT account (610119931565), this should work - check bucket policy"
    echo "  3. Run: aws iam list-attached-user-policies --user-name YOUR_USER"
    exit 1
fi
echo ""

# Test 3: Read specific object
echo "Test 3: Read object from partner prefix"
echo "  Command: aws s3 cp s3://${BUCKET_NAME}/${PREFIX}/test-access.txt -"
if aws s3 cp "s3://${BUCKET_NAME}/${PREFIX}/test-access.txt" - --no-cli-pager 2>&1; then
    echo "  ✓ Successfully read object"
else
    EXIT_CODE=$?
    echo "  ✗ FAILED to read object (exit code: $EXIT_CODE)"
    echo "  Note: File may not exist. Create it first if testing from partner account."
    exit 1
fi
echo ""

# Test 4: List objects with API call
echo "Test 4: List objects using S3 API"
echo "  Command: aws s3api list-objects-v2 --bucket ${BUCKET_NAME} --prefix ${PREFIX}/"
if aws s3api list-objects-v2 --bucket "${BUCKET_NAME}" --prefix "${PREFIX}/" --no-cli-pager 2>&1 | head -20; then
    echo "  ✓ Successfully listed objects via API"
else
    EXIT_CODE=$?
    echo "  ✗ FAILED to list via API (exit code: $EXIT_CODE)"
    exit 1
fi
echo ""

# Test 5: Try to access another partner's prefix (should fail)
if [[ "$PREFIX" == "uaitestpartner1" ]]; then
    OTHER_PREFIX="uaitestpartner2"
else
    OTHER_PREFIX="uaitestpartner1"
fi

echo "Test 5: Attempt to access different prefix (should fail for partners)"
echo "  Command: aws s3 ls s3://${BUCKET_NAME}/${OTHER_PREFIX}/"
if aws s3 ls "s3://${BUCKET_NAME}/${OTHER_PREFIX}/" --no-cli-pager 2>&1 | head -3; then
    echo "  ⚠️  Access succeeded - you are likely the bucket owner"
else
    EXIT_CODE=$?
    if [[ $EXIT_CODE -eq 254 ]] || [[ $EXIT_CODE -eq 1 ]]; then
        echo "  ✓ Access Denied as expected (proper isolation)"
    else
        echo "  ? Unexpected result (exit code: $EXIT_CODE)"
    fi
fi
echo ""

echo "========================================="
echo "Summary"
echo "========================================="
echo "Account: $ACCOUNT_ID"
if [[ "$ACCOUNT_ID" == "610119931565" ]]; then
    echo "Role: Bucket Owner (MIT)"
    echo "Expected: All tests should pass"
elif [[ "$ACCOUNT_ID" == "713545616749" ]]; then
    echo "Role: Partner Account"
    echo "Expected: Prefix access succeeds, root/other prefixes denied"
else
    echo "Role: Unknown account"
    echo "This account is not configured in the bucket policy"
fi
echo ""
echo "✅ Tests completed!"
