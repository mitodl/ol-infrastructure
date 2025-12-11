"""Pulumi components for configuring AWS Backup for EKS clusters."""

import json

import pulumi_aws as aws
from pulumi import ResourceOptions

from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION


def setup_eks_backup(
    cluster_name: str,
    cluster,
    aws_config,
):
    """
    Set up AWS Backup for an EKS cluster.

    This creates:
    - A Backup Vault to store backups
    - A Backup Plan with backup rules
    - A Backup Selection to target the EKS cluster
    """
    ############################################################
    # Create AWS Backup Vault
    ############################################################
    backup_vault = aws.backup.Vault(
        f"{cluster_name}-eks-backup-vault",
        name=f"{cluster_name}-eks-backup-vault",
        tags={
            **aws_config.tags,
            "Name": f"{cluster_name}-eks-backup-vault",
        },
        opts=ResourceOptions(depends_on=[cluster]),
    )

    ############################################################
    # Create IAM Role for AWS Backup
    ############################################################
    backup_role_assume_policy = {
        "Version": IAM_POLICY_VERSION,
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "backup.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    backup_role = aws.iam.Role(
        f"{cluster_name}-eks-backup-role",
        name=f"{cluster_name}-eks-backup-role",
        assume_role_policy=json.dumps(backup_role_assume_policy),
        path=f"/ol-infrastructure/eks/{cluster_name}/backup/",
        tags=aws_config.tags,
    )

    # Attach AWS managed backup policy to the role
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-eks-backup-role-policy-attachment",
        role=backup_role.name,
        policy_arn="arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup",
        opts=ResourceOptions(parent=backup_role),
    )

    # Attach AWS managed restore policy to the role
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-eks-backup-role-restore-policy-attachment",
        role=backup_role.name,
        policy_arn="arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores",
        opts=ResourceOptions(parent=backup_role),
    )

    ############################################################
    # Create AWS Backup Plan
    ############################################################
    backup_plan = aws.backup.Plan(
        f"{cluster_name}-eks-backup-plan",
        name=f"{cluster_name}-eks-backup-plan",
        rules=[
            aws.backup.PlanRuleArgs(
                rule_name="daily-backup",
                target_vault_name=backup_vault.name,
                schedule="cron(0 5 ? * * *)",  # Daily at 5 AM UTC
                start_window=60,  # Start within 60 minutes of scheduled time
                completion_window=120,  # Complete within 120 minutes of start
                lifecycle=aws.backup.PlanRuleLifecycleArgs(
                    delete_after=14,  # Delete backups after 14 days
                ),
            )
        ],
        tags={
            **aws_config.tags,
            "Name": f"{cluster_name}-eks-backup-plan",
        },
        opts=ResourceOptions(depends_on=[backup_vault]),
    )

    ############################################################
    # Create AWS Backup Selection
    ############################################################
    backup_selection = aws.backup.Selection(
        f"{cluster_name}-eks-backup-selection",
        name=f"{cluster_name}-eks-backup-selection",
        plan_id=backup_plan.id,
        iam_role_arn=backup_role.arn,
        resources=[
            cluster.core.cluster.arn,
        ],
        opts=ResourceOptions(depends_on=[backup_plan, backup_role, cluster]),
    )

    return {
        "vault": backup_vault,
        "plan": backup_plan,
        "selection": backup_selection,
        "role": backup_role,
    }
