// Refactored endpoint example: async/await, explicit error paths, and secret placeholders.
// Configure these values through your approved secrets manager or runtime environment.
const API_HOST = process.env.PAYMENTS_API_HOST ?? 'api.example.com';
const API_TOKEN = process.env.PAYMENTS_API_TOKEN; // e.g. injected from a secrets manager
const DATABASE_URL = process.env.PAYMENTS_DATABASE_URL; // encrypted secret reference at deployment

if (!API_TOKEN) {
  throw new Error('Missing PAYMENTS_API_TOKEN. Inject it through the approved secrets manager.');
}

/**
 * Retrieves a payment status from the service API.
 * @param {string} paymentId
 * @returns {Promise<object>}
 */
async function getPaymentStatus(paymentId) {
  const url = new URL(`/v1/payments/${encodeURIComponent(paymentId)}`, `https://${API_HOST}`);

  let response;
  try {
    response = await fetch(url, {
      headers: {
        Authorization: `Bearer ${API_TOKEN}`,
        Accept: 'application/json',
      },
      signal: AbortSignal.timeout(10_000),
    });
  } catch (cause) {
    throw new Error('Payment API request could not be completed.', { cause });
  }

  if (!response.ok) {
    throw new Error(`Payment API request failed with HTTP ${response.status}.`);
  }

  try {
    return await response.json();
  } catch (cause) {
    throw new Error('Payment API returned invalid JSON.', { cause });
  }
}

async function main() {
  const payment = await getPaymentStatus('pay_123');
  console.log('Payment status:', payment.status);
}

main().catch((error) => {
  console.error('Unable to retrieve payment status:', error.message);
  process.exitCode = 1;
});

// DATABASE_URL is deliberately not logged or interpolated. Use it only in a
// database client initialized with TLS and credentials supplied at runtime.
void DATABASE_URL;
