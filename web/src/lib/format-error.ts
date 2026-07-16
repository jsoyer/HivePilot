import { ApiAuthError, ApiError, ApiForbiddenError } from './api'

/**
 * Human-readable error message for any error an `apiFetch`-based call can
 * throw. Used as the default message in `AsyncSection`'s error state; a
 * view can still special-case a specific error type itself (e.g. the Mem0
 * view's dedicated `ApiForbiddenError` -> "requires an admin token" card).
 */
export function describeApiError(error: unknown): string {
  if (error instanceof ApiAuthError) {
    return 'Your session is no longer valid — returning to the token gate.'
  }
  if (error instanceof ApiForbiddenError) {
    return 'This view requires a higher-privilege (admin) token.'
  }
  if (error instanceof ApiError) {
    return error.message
  }
  if (error instanceof Error) {
    return error.message
  }
  return 'Something went wrong loading this data.'
}
