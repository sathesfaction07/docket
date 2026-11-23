#!/usr/bin/env bash
set -e

# ─────────────────────────────────────────────────────────────────────────────
# Docket Setup Script
# Detects OS, creates/reuses virtual environment, installs dependencies,
# optionally provisions cloud resources (AWS DynamoDB or Google Firestore),
# and starts the application.
# ─────────────────────────────────────────────────────────────────────────────

APP_NAME="docket"
VENV_DIR=".venv"
PYTHON_MIN="3.11"

# ── OS Detection ─────────────────────────────────────────────────────────────

detect_os() {
    case "$(uname -s)" in
        Linux*)   OS="linux" ;;
        Darwin*)  OS="macos" ;;
        MINGW*|MSYS*|CYGWIN*) OS="windows" ;;
        *)        OS="unknown" ;;
    esac
    echo "Detected OS: $OS"
}

# ── Python Detection ─────────────────────────────────────────────────────────

find_python() {
    for cmd in python3.12 python3.11 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            version=$("$cmd" --version 2>&1 | grep -oP '\d+\.\d+')
            if [ "$(echo "$version >= $PYTHON_MIN" | bc -l 2>/dev/null || python3 -c "print(1 if $version >= $PYTHON_MIN else 0)")" = "1" ]; then
                PYTHON="$cmd"
                echo "Using Python: $PYTHON ($version)"
                return 0
            fi
        fi
    done
    echo "ERROR: Python >= $PYTHON_MIN not found. Please install Python $PYTHON_MIN+"
    exit 1
}

# ── Virtual Environment ──────────────────────────────────────────────────────

setup_venv() {
    if [ -d "$VENV_DIR" ]; then
        echo "Virtual environment exists at $VENV_DIR — reusing."
    else
        echo "Creating virtual environment..."
        $PYTHON -m venv "$VENV_DIR"
        echo "Virtual environment created at $VENV_DIR"
    fi

    # Activate
    if [ "$OS" = "windows" ]; then
        source "$VENV_DIR/Scripts/activate"
    else
        source "$VENV_DIR/bin/activate"
    fi

    # Upgrade pip
    pip install --upgrade pip --quiet

    # Install project dependencies
    echo "Installing dependencies..."
    pip install -e ".[dev]" --quiet
    echo "Dependencies installed."
}

# ── Cloud Provider Selection ─────────────────────────────────────────────────

select_cloud_provider() {
    echo ""
    echo "═══════════════════════════════════════════════════════════════"
    echo "  Choose where cases are stored:"
    echo ""
    echo "  1) AWS (DynamoDB)"
    echo "  2) Google Cloud (Firestore)"
    echo "  3) In-memory (no cloud account; data lasts until the API stops)"
    echo "═══════════════════════════════════════════════════════════════"
    echo ""
    read -p "Selection [1/2/3]: " CLOUD_CHOICE

    case "$CLOUD_CHOICE" in
        1) setup_aws ;;
        2) setup_gcp ;;
        3) setup_memory ;;
        *) echo "Invalid choice. Using the in-memory backend."; setup_memory ;;
    esac
}

# ── AWS Setup ────────────────────────────────────────────────────────────────

setup_aws() {
    echo ""
    echo "── AWS DynamoDB Setup ──────────────────────────────────────────"

    # Check AWS CLI
    if ! command -v aws &>/dev/null; then
        echo "ERROR: AWS CLI not found. Install: https://aws.amazon.com/cli/"
        exit 1
    fi

    read -p "AWS Account ID: " AWS_ACCOUNT_ID
    read -p "AWS Region [us-east-1]: " AWS_REGION
    AWS_REGION=${AWS_REGION:-us-east-1}

    echo ""
    echo "Creating IAM role and DynamoDB table..."

    ROLE_NAME="Docket-AppRole"
    TABLE_NAME="Docket"
    POLICY_NAME="Docket-DynamoDBAccess"

    # Trust policy for local execution (user assumes role)
    TRUST_POLICY=$(cat <<EOF
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"AWS": "arn:aws:iam::${AWS_ACCOUNT_ID}:root"},
        "Action": "sts:AssumeRole"
    }]
}
EOF
)

    # DynamoDB access policy
    ACCESS_POLICY=$(cat <<EOF
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Action": [
            "dynamodb:PutItem",
            "dynamodb:GetItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "dynamodb:Query",
            "dynamodb:Scan",
            "dynamodb:BatchWriteItem",
            "dynamodb:DescribeTable"
        ],
        "Resource": [
            "arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${TABLE_NAME}",
            "arn:aws:dynamodb:${AWS_REGION}:${AWS_ACCOUNT_ID}:table/${TABLE_NAME}/index/*"
        ]
    }]
}
EOF
)

    # Create IAM role (skip if exists)
    if aws iam get-role --role-name "$ROLE_NAME" &>/dev/null 2>&1; then
        echo "Role $ROLE_NAME already exists — skipping."
    else
        aws iam create-role \
            --role-name "$ROLE_NAME" \
            --assume-role-policy-document "$TRUST_POLICY" \
            --description "Docket application role for DynamoDB access" \
            --region "$AWS_REGION" --output text --query 'Role.Arn'
        echo "Created role: $ROLE_NAME"
    fi

    # Attach inline policy
    aws iam put-role-policy \
        --role-name "$ROLE_NAME" \
        --policy-name "$POLICY_NAME" \
        --policy-document "$ACCESS_POLICY" 2>/dev/null
    echo "Attached DynamoDB access policy."

    # Create DynamoDB table (skip if exists)
    if aws dynamodb describe-table --table-name "$TABLE_NAME" --region "$AWS_REGION" &>/dev/null 2>&1; then
        echo "Table $TABLE_NAME already exists — skipping."
    else
        aws dynamodb create-table \
            --table-name "$TABLE_NAME" \
            --attribute-definitions \
                AttributeName=case_id,AttributeType=S \
                AttributeName=status,AttributeType=S \
                AttributeName=assignee,AttributeType=S \
                AttributeName=category,AttributeType=S \
                AttributeName=created_at,AttributeType=S \
            --key-schema AttributeName=case_id,KeyType=HASH \
            --global-secondary-indexes \
                "[{\"IndexName\":\"StatusDateIndex\",\"KeySchema\":[{\"AttributeName\":\"status\",\"KeyType\":\"HASH\"},{\"AttributeName\":\"created_at\",\"KeyType\":\"RANGE\"}],\"Projection\":{\"ProjectionType\":\"ALL\"}},
                  {\"IndexName\":\"AssigneeDateIndex\",\"KeySchema\":[{\"AttributeName\":\"assignee\",\"KeyType\":\"HASH\"},{\"AttributeName\":\"created_at\",\"KeyType\":\"RANGE\"}],\"Projection\":{\"ProjectionType\":\"ALL\"}},
                  {\"IndexName\":\"CategoryIndex\",\"KeySchema\":[{\"AttributeName\":\"category\",\"KeyType\":\"HASH\"},{\"AttributeName\":\"created_at\",\"KeyType\":\"RANGE\"}],\"Projection\":{\"ProjectionType\":\"ALL\"}}]" \
            --billing-mode PAY_PER_REQUEST \
            --region "$AWS_REGION" \
            --output text --query 'TableDescription.TableArn'

        echo "Created DynamoDB table: $TABLE_NAME"
        echo "Waiting for table to become active..."
        aws dynamodb wait table-exists --table-name "$TABLE_NAME" --region "$AWS_REGION"
        echo "Table is active."
    fi

    # Write env file
    cat > .env <<EOF
CF_DATABASE_BACKEND=dynamodb
CF_AWS_REGION=${AWS_REGION}
CF_DYNAMODB_TABLE=${TABLE_NAME}
EOF
    echo ""
    echo "AWS setup complete. Configuration written to .env"
}

# ── In-memory Setup ──────────────────────────────────────────────────────────

setup_memory() {
    cat > .env <<EOF
CF_DATABASE_BACKEND=memory
EOF
    echo "In-memory backend configured in .env. Nothing is persisted; restart = empty."
}

# ── Google Cloud Setup ───────────────────────────────────────────────────────

setup_gcp() {
    echo ""
    echo "── Google Cloud Firestore Setup ───────────────────────────────"

    # Check gcloud CLI
    if ! command -v gcloud &>/dev/null; then
        echo "ERROR: gcloud CLI not found. Install: https://cloud.google.com/sdk/docs/install"
        exit 1
    fi

    read -p "GCP Project ID: " GCP_PROJECT
    read -p "GCP Region [us-central1]: " GCP_REGION
    GCP_REGION=${GCP_REGION:-us-central1}

    echo ""
    echo "Configuring Firestore..."

    # Set project
    gcloud config set project "$GCP_PROJECT" --quiet

    # Enable Firestore API
    gcloud services enable firestore.googleapis.com --quiet 2>/dev/null
    echo "Firestore API enabled."

    # Create Firestore database (if not exists)
    if gcloud firestore databases describe --project="$GCP_PROJECT" &>/dev/null 2>&1; then
        echo "Firestore database already exists — skipping."
    else
        gcloud firestore databases create \
            --project="$GCP_PROJECT" \
            --location="$GCP_REGION" \
            --type=firestore-native \
            --quiet 2>/dev/null || echo "Database may already exist."
        echo "Firestore database created."
    fi

    # Create service account for the app
    SA_NAME="caseforge-app"
    SA_EMAIL="${SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"

    if gcloud iam service-accounts describe "$SA_EMAIL" &>/dev/null 2>&1; then
        echo "Service account $SA_NAME already exists — skipping."
    else
        gcloud iam service-accounts create "$SA_NAME" \
            --display-name="Docket Application" \
            --quiet
        echo "Created service account: $SA_EMAIL"
    fi

    # Grant Firestore access
    gcloud projects add-iam-policy-binding "$GCP_PROJECT" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="roles/datastore.user" \
        --quiet &>/dev/null
    echo "Granted Firestore access to service account."

    # Download key (if not exists)
    KEY_FILE="gcp-credentials.json"
    if [ ! -f "$KEY_FILE" ]; then
        gcloud iam service-accounts keys create "$KEY_FILE" \
            --iam-account="$SA_EMAIL" \
            --quiet
        echo "Service account key saved to $KEY_FILE"
    fi

    # Install Firestore dependency
    pip install "docket[firestore]" --quiet

    # Write env file
    cat > .env <<EOF
CF_DATABASE_BACKEND=firestore
CF_GCP_PROJECT=${GCP_PROJECT}
CF_FIRESTORE_COLLECTION=cases
GOOGLE_APPLICATION_CREDENTIALS=${KEY_FILE}
EOF
    echo ""
    echo "Google Cloud setup complete. Configuration written to .env"
}

# ── Run Application ──────────────────────────────────────────────────────────

run_app() {
    echo ""
    echo "═══════════════════════════════════════════════════════════════"
    echo "  Starting Docket..."
    echo "  API:      http://localhost:8000"
    echo "  Frontend: http://localhost:3000 (open separately)"
    echo "═══════════════════════════════════════════════════════════════"
    echo ""

    # Load .env if exists
    if [ -f .env ]; then
        export $(grep -v '^#' .env | xargs)
    fi

    # Start frontend server in background
    if command -v python3 &>/dev/null; then
        python3 -m http.server 3000 --directory frontend &>/dev/null &
        FRONTEND_PID=$!
        echo "Frontend serving at http://localhost:3000 (PID: $FRONTEND_PID)"
    fi

    # Identity: with no tokens configured, run in single-user local mode as the OS user and
    # bind to loopback only. To share the API, set CF_API_TOKENS (token:user,...) and CF_HOST.
    if [ -z "${CF_API_TOKENS:-}" ] && [ -z "${CF_LOCAL_USER:-}" ]; then
        export CF_LOCAL_USER="$(whoami)"
        echo "No CF_API_TOKENS set: single-user local mode as '${CF_LOCAL_USER}'."
    fi
    HOST="${CF_HOST:-127.0.0.1}"
    if [ -n "${CF_LOCAL_USER:-}" ] && [ "$HOST" != "127.0.0.1" ] && [ "$HOST" != "localhost" ]; then
        echo "WARNING: CF_LOCAL_USER lets requests without a token act as '${CF_LOCAL_USER}'."
        echo "         Do not combine it with a non-loopback CF_HOST ($HOST)."
    fi

    # Start backend
    uvicorn case_forge.api:app --host "$HOST" --port 8000
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
    echo ""
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║              Docket — Setup & Run                         ║"
    echo "╚═══════════════════════════════════════════════════════════════╝"
    echo ""

    detect_os
    find_python
    setup_venv
    select_cloud_provider
    run_app
}

main "$@"
