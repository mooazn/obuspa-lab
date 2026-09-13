/*
 * vhal.c - see vhal.h. Deliberately small and dependency-free so that it can
 * be dropped into any vendor build as a single source file.
 */

#include "vhal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/time.h>

#define DEFAULT_SOCK "/run/vdev/vdev.sock"
#define TIMEOUT_SECS 2

/* ------------------------------------------------------------------ transport */

static int connect_device(int timeout_secs)
{
    const char *path = getenv("VDEV_SOCK");
    struct sockaddr_un addr;
    struct timeval tv = { timeout_secs, 0 };
    int fd;

    if (path == NULL || *path == '\0')
        path = DEFAULT_SOCK;

    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
        return -1;

    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path, sizeof addr.sun_path - 1);

    if (connect(fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        close(fd);
        return -1;
    }
    if (timeout_secs > 0) {
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    }
    return fd;
}

static int send_all(int fd, const char *buf, size_t len)
{
    while (len > 0) {
        ssize_t n = send(fd, buf, len, MSG_NOSIGNAL);
        if (n <= 0)
            return -1;
        buf += n;
        len -= (size_t)n;
    }
    return 0;
}

/* Reads one newline-terminated line into a malloc'd buffer. */
static char *read_line(int fd)
{
    size_t cap = 512, len = 0;
    char *buf = malloc(cap);

    if (buf == NULL)
        return NULL;
    for (;;) {
        char c;
        ssize_t n = recv(fd, &c, 1, 0);
        if (n <= 0) {
            free(buf);
            return NULL;
        }
        if (c == '\n')
            break;
        if (len + 2 > cap) {
            char *grown = realloc(buf, cap *= 2);
            if (grown == NULL) {
                free(buf);
                return NULL;
            }
            buf = grown;
        }
        buf[len++] = c;
    }
    buf[len] = '\0';
    return buf;
}

/* ------------------------------------------------------------------ JSON, minimal */

/* Appends a JSON string literal (with quotes) for `s` to `out`. */
static int json_quote(const char *s, char *out, size_t size)
{
    size_t n = 0;
#define PUT(ch) do { if (n + 1 >= size) return -1; out[n++] = (ch); } while (0)
    PUT('"');
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') { PUT('\\'); PUT((char)c); }
        else if (c == '\n') { PUT('\\'); PUT('n'); }
        else if (c == '\r') { PUT('\\'); PUT('r'); }
        else if (c == '\t') { PUT('\\'); PUT('t'); }
        else if (c < 0x20) {
            char esc[7];
            snprintf(esc, sizeof esc, "\\u%04x", c);
            for (const char *e = esc; *e; e++) PUT(*e);
        } else PUT((char)c);
    }
    PUT('"');
#undef PUT
    out[n] = '\0';
    return 0;
}

static void put_utf8(unsigned cp, char *out, size_t *n, size_t size)
{
    char tmp[4];
    int len = 0;
    if (cp < 0x80) { tmp[0] = (char)cp; len = 1; }
    else if (cp < 0x800) { tmp[0] = (char)(0xC0 | (cp >> 6)); tmp[1] = (char)(0x80 | (cp & 0x3F)); len = 2; }
    else if (cp < 0x10000) { tmp[0] = (char)(0xE0 | (cp >> 12)); tmp[1] = (char)(0x80 | ((cp >> 6) & 0x3F)); tmp[2] = (char)(0x80 | (cp & 0x3F)); len = 3; }
    else { tmp[0] = (char)(0xF0 | (cp >> 18)); tmp[1] = (char)(0x80 | ((cp >> 12) & 0x3F)); tmp[2] = (char)(0x80 | ((cp >> 6) & 0x3F)); tmp[3] = (char)(0x80 | (cp & 0x3F)); len = 4; }
    for (int i = 0; i < len; i++)
        if (*n + 1 < size) out[(*n)++] = tmp[i];
}

/* Parses a JSON string starting at `p` (pointing at the opening quote).
 * Writes the decoded text into `out` (truncating) and returns a pointer just
 * past the closing quote, or NULL on malformed input. `out` may be NULL to
 * skip. */
static const char *json_unquote(const char *p, char *out, size_t size)
{
    size_t n = 0;
    if (*p != '"')
        return NULL;
    for (p++; *p && *p != '"'; p++) {
        char c = *p;
        if (c == '\\') {
            p++;
            switch (*p) {
            case '"': c = '"'; break;
            case '\\': c = '\\'; break;
            case '/': c = '/'; break;
            case 'n': c = '\n'; break;
            case 'r': c = '\r'; break;
            case 't': c = '\t'; break;
            case 'b': c = '\b'; break;
            case 'f': c = '\f'; break;
            case 'u': {
                unsigned cp = 0;
                if (sscanf(p + 1, "%4x", &cp) != 1) return NULL;
                p += 4;
                if (cp >= 0xD800 && cp <= 0xDBFF && p[1] == '\\' && p[2] == 'u') {
                    unsigned lo = 0;
                    if (sscanf(p + 3, "%4x", &lo) == 1 && lo >= 0xDC00 && lo <= 0xDFFF) {
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        p += 6;
                    }
                }
                if (out) put_utf8(cp, out, &n, size);
                continue;
            }
            default: return NULL;
            }
        }
        if (out && n + 1 < size)
            out[n++] = c;
    }
    if (*p != '"')
        return NULL;
    if (out)
        out[n < size ? n : size - 1] = '\0';
    return p + 1;
}

static const char *skip_ws(const char *p)
{
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
    return p;
}

/* Skips any JSON value; used for members we do not care about. */
static const char *skip_value(const char *p)
{
    p = skip_ws(p);
    if (*p == '"')
        return json_unquote(p, NULL, 0);
    if (*p == '{' || *p == '[') {
        int depth = 0;
        for (; *p; p++) {
            if (*p == '"') { p = json_unquote(p, NULL, 0); if (!p) return NULL; p--; continue; }
            if (*p == '{' || *p == '[') depth++;
            else if (*p == '}' || *p == ']') { if (--depth == 0) return p + 1; }
        }
        return NULL;
    }
    while (*p && *p != ',' && *p != '}' && *p != ']') p++;
    return p;
}

/* Looks up a top-level member of a JSON object. Returns 1 and fills `out`
 * for a string, 0 for null (out untouched), 2 for any other value (raw text
 * copied), -1 if absent or malformed. */
static int json_member(const char *obj, const char *name, char *out, size_t size)
{
    const char *p = skip_ws(obj);
    char key[256];

    if (*p != '{')
        return -1;
    p = skip_ws(p + 1);
    while (*p && *p != '}') {
        p = json_unquote(p, key, sizeof key);
        if (!p) return -1;
        p = skip_ws(p);
        if (*p != ':') return -1;
        p = skip_ws(p + 1);
        if (strcmp(key, name) == 0) {
            if (*p == '"')
                return json_unquote(p, out, size) ? 1 : -1;
            if (strncmp(p, "null", 4) == 0)
                return 0;
            {
                const char *end = skip_value(p);
                size_t len = end ? (size_t)(end - p) : 0;
                if (len >= size) len = size - 1;
                memcpy(out, p, len);
                out[len] = '\0';
                return 2;
            }
        }
        p = skip_value(p);
        if (!p) return -1;
        p = skip_ws(p);
        if (*p == ',') p = skip_ws(p + 1);
    }
    return -1;
}

/* ------------------------------------------------------------------ API */

static int request(const char *op, const char *key, const char *value,
                   int with_value, char **response)
{
    char qkey[600], qval[4400], line[5200];
    int fd, rc;

    if (json_quote(key, qkey, sizeof qkey) < 0)
        return -1;
    if (with_value) {
        if (value == NULL)
            strcpy(qval, "null");
        else if (json_quote(value, qval, sizeof qval) < 0)
            return -1;
        snprintf(line, sizeof line, "{\"op\":\"%s\",\"key\":%s,\"value\":%s}\n", op, qkey, qval);
    } else {
        snprintf(line, sizeof line, "{\"op\":\"%s\",\"key\":%s}\n", op, qkey);
    }

    fd = connect_device(TIMEOUT_SECS);
    if (fd < 0)
        return -1;
    rc = send_all(fd, line, strlen(line));
    *response = rc == 0 ? read_line(fd) : NULL;
    close(fd);
    return *response ? 0 : -1;
}

int vhal_get(const char *key, char *value, size_t size)
{
    char *response;
    char ok[8];
    int rc;

    if (key == NULL || value == NULL || size == 0)
        return -1;
    if (request("hal_get", key, NULL, 0, &response) < 0)
        return -1;

    rc = -1;
    if (json_member(response, "ok", ok, sizeof ok) == 2 && strcmp(ok, "true") == 0) {
        int kind = json_member(response, "value", value, size);
        rc = (kind == 1) ? 0 : (kind == 0 ? 1 : -1);
    }
    free(response);
    return rc;
}

int vhal_set(const char *key, const char *value)
{
    char *response;
    char ok[8];
    int rc;

    if (key == NULL)
        return -1;
    if (request("hal_set", key, value, 1, &response) < 0)
        return -1;
    rc = (json_member(response, "ok", ok, sizeof ok) == 2 && strcmp(ok, "true") == 0) ? 0 : -1;
    free(response);
    return rc;
}

int vhal_watch(const char *prefix, vhal_watch_cb callback, void *ctx)
{
    char qprefix[600], line[700];
    int fd;

    if (callback == NULL)
        return -1;
    if (json_quote(prefix ? prefix : "", qprefix, sizeof qprefix) < 0)
        return -1;
    snprintf(line, sizeof line, "{\"op\":\"hal_watch\",\"prefix\":%s}\n", qprefix);

    fd = connect_device(0);         /* no timeout: this blocks by design */
    if (fd < 0)
        return -1;
    if (send_all(fd, line, strlen(line)) < 0) {
        close(fd);
        return -1;
    }

    for (;;) {
        char *entry = read_line(fd);
        char key[256], value[4096];
        int kind;

        if (entry == NULL)
            break;              /* device went away */
        if (json_member(entry, "key", key, sizeof key) == 1) {
            kind = json_member(entry, "value", value, sizeof value);
            if (kind == 1)
                callback(key, value, ctx);
            else if (kind == 0)
                callback(key, NULL, ctx);
        }
        free(entry);
    }
    close(fd);
    return -1;
}
