import { describe, expect, it } from 'vitest'
import { ApiAuthError, ApiError, ApiForbiddenError } from './api'
import { describeApiError } from './format-error'

describe('describeApiError', () => {
  it('describes ApiAuthError as a session/reconnect message', () => {
    expect(describeApiError(new ApiAuthError(401))).toMatch(/session|token gate/i)
  })

  it('describes ApiForbiddenError as a privilege message', () => {
    expect(describeApiError(new ApiForbiddenError())).toMatch(/admin|privilege|role/i)
  })

  it('describes ApiError using its own message', () => {
    const error = new ApiError(500, 'boom')
    expect(describeApiError(error)).toBe(error.message)
  })

  it('describes a plain Error using its message', () => {
    expect(describeApiError(new Error('network down'))).toBe('network down')
  })

  it('falls back to a generic message for a non-Error value', () => {
    expect(describeApiError('not an error')).toMatch(/something went wrong/i)
  })
})
