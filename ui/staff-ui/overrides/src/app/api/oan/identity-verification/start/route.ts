import { NextRequest } from "next/server";
import { proxyToBackend } from "@/app/api/_lib/backend-proxy";

// Starts a Fayda (eSignet) verification of the FIN on an intake form. The
// staff member is taken from the session by the staff API, not sent from here.
export async function POST(request: NextRequest) {
    return proxyToBackend({
        req: request,
        targetEndpoint: "/oan/identity-verification/start",
        buildPayload: (body) => ({
            request_payload: {
                register_id: body.register_id,
                internal_record_id: body.internal_record_id || null,
            },
        }),
    });
}
