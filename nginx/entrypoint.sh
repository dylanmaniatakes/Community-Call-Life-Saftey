#!/bin/sh
# ── Community Call — Nginx entrypoint ────────────────────────────────────────
# Generates a 2-tier PKI on first boot:
#
#   ca.crt  — Local CA certificate (CA:TRUE).  Users install this once.
#             Served at GET /sslcert for easy download.
#   ca.key  — CA private key (stays on the server, never downloaded).
#   cert.pem / key.pem — Server certificate signed by the CA (CA:FALSE).
#             Used by nginx for TLS. Clients trust it because they trust the CA.
#
# Why two tiers?
#   Installing a CA cert tells the OS "trust everything this CA signs."
#   Installing a self-signed server cert (what we used to do) is different —
#   the OS only trusts that one specific cert, and Android/iOS reject it unless
#   it has basicConstraints=CA:TRUE, which a server cert must NOT have.
#   The 2-tier split lets each cert be correct for its role.
#
# Environment variables (all optional):
#   SERVER_NAME  — hostname for the server cert CN / SAN (default: community-call)
#   SERVER_IP    — extra IP SAN, e.g. your LAN address (e.g. 192.168.1.100)
#   CERT_DAYS    — server cert validity in days (default: 3650 = ~10 years)

set -e

SSL_DIR="/etc/nginx/ssl"
CA_KEY="${SSL_DIR}/ca.key"
CA_CERT="${SSL_DIR}/ca.crt"
SERVER_KEY="${SSL_DIR}/key.pem"
SERVER_CERT="${SSL_DIR}/cert.pem"
SERVER_CSR="${SSL_DIR}/server.csr"
CA_CNF="${SSL_DIR}/ca.cnf"
SRV_CNF="${SSL_DIR}/server.cnf"

SERVER_NAME="${SERVER_NAME:-community-call}"
CERT_DAYS="${CERT_DAYS:-3650}"

mkdir -p "$SSL_DIR"

# ── Step 1: Generate the local CA (once — persists across container restarts) ──
if [ ! -f "$CA_KEY" ] || [ ! -f "$CA_CERT" ]; then
    echo "[nginx] Generating local CA..."

    cat > "$CA_CNF" <<EOF
[req]
default_bits       = 4096
prompt             = no
default_md         = sha256
distinguished_name = dn
x509_extensions    = v3_ca

[dn]
CN = Community Call Local CA
O  = Community Call System
C  = US

[v3_ca]
# CA:TRUE is what makes Android/iOS/Mac accept this as an installable root CA.
basicConstraints     = critical,CA:TRUE
keyUsage             = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
EOF

    openssl req -x509 -newkey rsa:4096 \
        -keyout "$CA_KEY" \
        -out    "$CA_CERT" \
        -days   3650 \
        -nodes \
        -config "$CA_CNF" \
        2>/dev/null

    echo "[nginx] CA certificate written to ${CA_CERT}"
    echo "[nginx] Install this CA cert once on each device — visit /sslcert for instructions."
    echo ""
fi

# ── Step 2: Generate / renew the server certificate signed by the CA ──────────
# Regenerate if the server cert is missing or the CA was just (re)created.
if [ ! -f "$SERVER_KEY" ] || [ ! -f "$SERVER_CERT" ]; then
    echo "[nginx] Generating server certificate..."

    # Build Subject Alternative Names dynamically
    ALT_NAMES="DNS.1 = localhost\nDNS.2 = ${SERVER_NAME}\nIP.1 = 127.0.0.1"
    if [ -n "$SERVER_IP" ]; then
        ALT_NAMES="${ALT_NAMES}\nIP.2 = ${SERVER_IP}"
        echo "[nginx] Adding SAN IP: ${SERVER_IP}"
    fi

    cat > "$SRV_CNF" <<EOF
[req]
default_bits       = 2048
prompt             = no
default_md         = sha256
distinguished_name = dn
req_extensions     = v3_req

[dn]
CN = ${SERVER_NAME}
O  = Community Call System
C  = US

[v3_req]
subjectAltName   = @alt_names
# CA:FALSE — this is a server cert, not a CA.
basicConstraints = critical,CA:FALSE
keyUsage         = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth

[alt_names]
$(printf "$ALT_NAMES")
EOF

    # Generate server private key + CSR
    openssl req -newkey rsa:2048 \
        -keyout "$SERVER_KEY" \
        -out    "$SERVER_CSR" \
        -nodes \
        -config "$SRV_CNF" \
        2>/dev/null

    # Sign the CSR with our local CA
    openssl x509 -req \
        -in      "$SERVER_CSR" \
        -CA      "$CA_CERT" \
        -CAkey   "$CA_KEY" \
        -CAcreateserial \
        -out     "$SERVER_CERT" \
        -days    "$CERT_DAYS" \
        -extfile "$SRV_CNF" \
        -extensions v3_req \
        2>/dev/null

    rm -f "$SERVER_CSR"

    echo "[nginx] Server certificate signed by local CA."
    echo "[nginx] Valid for ${CERT_DAYS} days."
    echo ""
    echo "┌─────────────────────────────────────────────────────────────────┐"
    echo "│  Install the CA cert on each device — visit /sslcert           │"
    echo "│  Set SERVER_IP=<your-LAN-ip> in .env so the cert covers the IP │"
    echo "└─────────────────────────────────────────────────────────────────┘"
    echo ""
fi

nginx -t
exec nginx -g 'daemon off;'
