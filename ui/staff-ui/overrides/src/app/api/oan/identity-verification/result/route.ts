import { NextRequest } from "next/server";
import { proxyToBackend } from "@/app/api/_lib/backend-proxy";

// The outcome of a Fayda verification and the verified values for the form.
export async function POST(request: NextRequest) {
    return proxyToBackend({
        req: request,
        targetEndpoint: "/oan/identity-verification/result",
        buildPayload: (body) => ({
            request_payload: { verification_id: body.verification_id },
        }),
    });
}
