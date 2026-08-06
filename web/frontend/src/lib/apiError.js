/**
 * What went wrong with a call to the run server.
 *
 * `offline` is the distinction the pages care about: there is no run server
 * behind this page at all, as opposed to one that answered and said no. The
 * static deployment is the first case for every request it ever makes, and it
 * is a normal condition there, not an error to apologise for.
 */
export class ApiError extends Error {
  constructor(message, { status = null, offline = false, cause = undefined } = {}) {
    super(message, { cause });
    this.name = "ApiError";
    this.status = status;
    this.offline = offline;
  }
}

/**
 * Whether a response body is worth parsing as an API response.
 *
 * A static host answers /api/* with whatever its SPA rewrite points at — for
 * this build, index.html, with status 200. So a 200 proves nothing; only the
 * content type separates "the API replied" from "a web server handed us the
 * app again". Anything that isn't JSON means there is no API here.
 */
export function looksLikeApiJson(contentType) {
  return /\bjson\b/i.test(contentType ?? "");
}
