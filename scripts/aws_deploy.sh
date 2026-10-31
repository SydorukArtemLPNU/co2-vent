#!/usr/bin/env bash
# Розгортання хмарних прогонів за запитом (після scripts/aws_setup.sh):
#   AWS_PROFILE=lpnu [BUDGET_EMAIL=...] scripts/aws_deploy.sh
# 1) S3-бакет (артефакти, результати runs/ зберігаються 30 днів), блокування публічного доступу;
# 2) сертифікати з certs/ -> SSM Parameter Store (SecureString);
# 3) образ cloud/Dockerfile (linux/arm64) -> ECR (зберігаються 3 останні);
# 4) zip Lambda -> S3; 5) стек CloudFormation cloud/stack.yaml.
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_PROFILE="${AWS_PROFILE:-lpnu}"
REGION="$(aws configure get region)"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="co2vent-${ACCOUNT}-${REGION}"
REPO=co2vent
STACK=co2vent-cloud
ENDPOINT="$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --query endpointAddress --output text)"
echo "account=$ACCOUNT region=$REGION bucket=$BUCKET"

# --- 1. бакет ---------------------------------------------------------------------
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
fi
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration \
  '{"Rules":[{"ID":"runs-30d","Status":"Enabled","Filter":{"Prefix":"runs/"},"Expiration":{"Days":30}},
             {"ID":"lambda-7d","Status":"Enabled","Filter":{"Prefix":"lambda/"},"Expiration":{"Days":7}}]}'

# --- 2. сертифікати в SSM ----------------------------------------------------------
put() { aws ssm put-parameter --name "$1" --type SecureString --overwrite --value "file://$2" >/dev/null; }
put /co2vent/certs/ca          certs/AmazonRootCA1.pem
put /co2vent/certs/server-cert certs/server.pem.crt
put /co2vent/certs/server-key  certs/server.private.pem.key
put /co2vent/certs/node-cert   certs/node.pem.crt
put /co2vent/certs/node-key    certs/node.private.pem.key

# --- 3. образ ---------------------------------------------------------------------
aws ecr describe-repositories --repository-names "$REPO" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "$REPO" --image-scanning-configuration scanOnPush=true >/dev/null
aws ecr put-lifecycle-policy --repository-name "$REPO" --lifecycle-policy-text \
  '{"rules":[{"rulePriority":1,"description":"keep 3","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":3},"action":{"type":"expire"}}]}' >/dev/null
REGISTRY="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
TAG="$(git rev-parse --short HEAD)$(git diff --quiet && echo "" || echo "-dirty")-$(date -u +%H%M%S)"
IMAGE="${REGISTRY}/${REPO}:${TAG}"
docker buildx build --platform linux/arm64 -f cloud/Dockerfile -t "$IMAGE" --push .
echo "image: $IMAGE"

# --- 4. Lambda --------------------------------------------------------------------
ZIP="$(mktemp -d)/control.zip"
(cd cloud/lambda && zip -q -X "$ZIP" handler.py page.html)
KEY="lambda/control-$(shasum -a 256 "$ZIP" | cut -c1-12).zip"
aws s3 cp "$ZIP" "s3://$BUCKET/$KEY" --quiet

# --- 5. стек ----------------------------------------------------------------------
VPC="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)"
SUBNETS="$(aws ec2 describe-subnets --filters Name=vpc-id,Values="$VPC" Name=default-for-az,Values=true \
           --query 'Subnets[].SubnetId' --output text | tr '\t' ',')"
aws cloudformation deploy --stack-name "$STACK" --template-file cloud/stack.yaml \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset --parameter-overrides \
  ImageUri="$IMAGE" LambdaCodeKey="$KEY" ResultsBucket="$BUCKET" IotEndpoint="$ENDPOINT" \
  VpcId="$VPC" SubnetIds="$SUBNETS"
aws cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].Outputs' --output table

# --- 6. бюджет-сповіщення (API бюджетів глобальний; ресурс CFN доступний лише в us-east-1) ---
if [ -n "${BUDGET_EMAIL:-}" ] && ! aws budgets describe-budget --account-id "$ACCOUNT" --budget-name co2vent-spend-alert >/dev/null 2>&1; then
  aws budgets create-budget --account-id "$ACCOUNT" \
    --budget '{"BudgetName":"co2vent-spend-alert","BudgetType":"COST","TimeUnit":"MONTHLY","BudgetLimit":{"Amount":"1","Unit":"USD"},"CostTypes":{"IncludeCredit":false,"IncludeRefund":false}}' \
    --notifications-with-subscribers "[{\"Notification\":{\"NotificationType\":\"ACTUAL\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":1,\"ThresholdType\":\"PERCENTAGE\"},\"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$BUDGET_EMAIL\"}]}]"
  echo "budget alert -> $BUDGET_EMAIL (any spend over \$0.01 without credits)"
fi
