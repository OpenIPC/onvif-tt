#!/usr/bin/env bash
# External verification that onvif-tt's PullPoint teardown actually
# invalidates subscriptions on the DUT, as the original plan required.
#
# Pattern:
#   1. Open a fresh PullPoint subscription, capture its
#      SubscriptionReference URL.
#   2. Unsubscribe it the same way the test harness does.
#   3. Send a raw SOAP Renew to the captured URL and inspect the result.
#
# A spec-compliant device returns env:Sender/ter:ResourceUnknownFault
# (or a HTTP-level error closing the connection). A non-compliant
# device (e.g. Xiongmai stock) silently accepts the Renew, proving the
# subscription was never destroyed — same signal as test EVENT-3-1-36.
#
# Usage:  ./scripts/verify-subscription-cleanup.sh HOST[:PORT] USER PASS
# Exit:   0 if subscription was correctly invalidated (or device closed
#         the URL); 1 if the device still answered Renew.

set -euo pipefail

TARGET="${1:?usage: verify-subscription-cleanup.sh HOST[:PORT] USER PASS}"
USER="${2:-admin}"
PASS="${3:-admin}"

step() { printf "\n=== %s ===\n" "$*"; }

step "1. Open a PullPoint subscription via onvif-tt's runtime"
SUB_URL=$(~/venvs/onvif-tt/bin/python - "$TARGET" "$USER" "$PASS" <<'PY'
import sys
host_port, user, password = sys.argv[1:4]
host, _, port = host_port.partition(":")
from onvif_tt.runtime.dut import DUT, DUTConfig
dut = DUT(DUTConfig(host=host, port=int(port) if port else 80,
                    user=user, password=password))
pp = dut.create_pullpoint("PT60S")
print(pp.subscription_url)
# Explicitly unsubscribe — the same code path the test harness uses.
pp.unsubscribe()
PY
)
echo "  subscription URL was: $SUB_URL"

step "2. Send a raw SOAP Renew to the (now-unsubscribed) URL"
RAW_RENEW='<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:wsa="http://www.w3.org/2005/08/addressing"
            xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">
  <s:Header>
    <wsa:Action>http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest</wsa:Action>
    <wsa:To>'"$SUB_URL"'</wsa:To>
  </s:Header>
  <s:Body>
    <wsnt:Renew>
      <wsnt:TerminationTime>PT60S</wsnt:TerminationTime>
    </wsnt:Renew>
  </s:Body>
</s:Envelope>'

HTTP_OUT=$(mktemp)
set +e
HTTP_CODE=$(curl -s -m 8 -o "$HTTP_OUT" -w "%{http_code}" \
  --digest --user "$USER:$PASS" \
  -X POST -H 'Content-Type: application/soap+xml; charset=utf-8' \
  --data "$RAW_RENEW" "$SUB_URL")
CURL_RC=$?
set -e
echo "  HTTP $HTTP_CODE  (curl rc=$CURL_RC, $(wc -c < "$HTTP_OUT") bytes received)"

step "3. Verdict"
RESP_BYTES=$(wc -c < "$HTTP_OUT")

if [[ "$CURL_RC" != "0" ]] || [[ "$HTTP_CODE" == "000" ]] || [[ "$RESP_BYTES" -eq 0 ]]; then
  echo "  PASS — DUT closed the connection on the unsubscribed URL"
  echo "  (device refused to handle Renew → subscription is effectively dead)"
  rm -f "$HTTP_OUT"
  exit 0
fi
if grep -qiE "Fault|ResourceUnknown|Sender" "$HTTP_OUT"; then
  echo "  PASS — DUT returned a SOAP Fault on the unsubscribed URL"
  echo "  (response preview):"
  head -c 400 "$HTTP_OUT"; echo
  rm -f "$HTTP_OUT"
  exit 0
fi
if grep -qiE "RenewResponse|TerminationTime" "$HTTP_OUT"; then
  echo "  FAIL — DUT silently renewed a destroyed subscription."
  echo "  This is the same non-conformance flagged by EVENT-3-1-36."
  echo "  (response preview):"
  head -c 400 "$HTTP_OUT"; echo
  rm -f "$HTTP_OUT"
  exit 1
fi
echo "  AMBIGUOUS — neither Fault nor RenewResponse seen."
echo "  (response preview):"
head -c 400 "$HTTP_OUT"; echo
rm -f "$HTTP_OUT"
exit 1
