// SPDX-License-Identifier: LGPL-2.1-or-later
// Derived from vrt49 tests/integration/sfp_udp_capture.c (vrt49 main 39d4294, LGPL-2.1-or-later).
//
// Full-rate VITA-49 UDP spooler for hopcap.  Every datagram is appended to a spool
// file with its arrival time (CLOCK_MONOTONIC and CLOCK_REALTIME, ns), peer address
// and length — see src/hopcap/spool.py for the record layout — so the host clock
// domain survives into the manifest and the Python decoder never has to keep up with
// the wire.  Per-datagram classification is kept only for the live summary.
//
// Protocol (same as the vrt49 helper): "ready rcvbuf=N" on stderr after bind;
// SIGUSR1 zeroes the counters (not the spool) and is acknowledged with "reset";
// SIGTERM/SIGINT drains the socket until it has been idle for 200 ms and prints one
// JSON summary on stdout.  --max-bytes makes the spooler stop and exit 3 instead of
// filling the disk.
//
// usage: udp_spool IPv4 PORT SPOOL_PATH [--max-bytes N] [--idle-ms N]
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define BATCH 64
#define DATAGRAM_MAX 9216
#define PEERS_MAX 8

struct peer { struct sockaddr_in addr; unsigned long long packets; };

struct __attribute__((packed)) spool_hdr {
    uint64_t mono_ns;
    uint64_t real_ns;
    uint32_t len;
    uint32_t peer_ip;   /* network-order IPv4 as a big-endian integer */
    uint16_t peer_port; /* host order */
    uint16_t pad;
};

static volatile sig_atomic_t stop_requested, reset_requested;
static void on_term(int s) { (void)s; stop_requested = 1; }
static void on_reset(int s) { (void)s; reset_requested = 1; }

static unsigned long long data_packets, context_packets, malformed, bytes, count_gaps, other_peers;
static unsigned long long spool_records, spool_bytes, spool_records_total, spool_bytes_total;
static unsigned peer_count;
static int last_count = -1, context_seen;
static struct peer peers[PEERS_MAX];

static void reset_accounting(void)
{
    data_packets = context_packets = malformed = bytes = count_gaps = other_peers = 0;
    spool_records = spool_bytes = 0;
    peer_count = 0;
    last_count = -1;
    context_seen = 0;
}

static void count_peer(const struct sockaddr_in *from)
{
    for (unsigned i = 0; i < peer_count; i++)
        if (peers[i].addr.sin_addr.s_addr == from->sin_addr.s_addr && peers[i].addr.sin_port == from->sin_port) {
            peers[i].packets++;
            return;
        }
    if (peer_count == PEERS_MAX) {
        other_peers++;
        return;
    }
    peers[peer_count].addr = *from;
    peers[peer_count++].packets = 1;
}

static void classify(const unsigned char *p, unsigned len, const struct sockaddr_in *from)
{
    uint32_t word0;
    count_peer(from);
    bytes += len;
    if (len < 8 || len % 4) {
        malformed++;
        return;
    }
    memcpy(&word0, p, 4);
    word0 = ntohl(word0);
    unsigned type = word0 >> 28;
    if ((word0 & 0xffff) * 4 != len || (type != 1 && type != 4) || (type == 1 && len < 28)) {
        malformed++;
        return;
    }
    if (type == 4) {
        context_packets++;
        if (!context_seen) {
            context_seen = 1;
            fputs("context\n", stderr);
        }
        last_count = (word0 >> 16) & 0xf;  /* Context shares the framer's packet counter */
        return;
    }
    data_packets++;
    int pc = (word0 >> 16) & 0xf;
    if (last_count >= 0 && pc != ((last_count + 1) & 0xf))
        count_gaps++;
    last_count = pc;
}

static uint64_t clock_ns(clockid_t id)
{
    struct timespec t;
    clock_gettime(id, &t);
    return (uint64_t)t.tv_sec * 1000000000ull + (uint64_t)t.tv_nsec;
}

static double now(void)
{
    return clock_ns(CLOCK_MONOTONIC) / 1e9;
}

static void print_summary(int rcvbuf, const char *stop_reason)
{
    char ip[INET_ADDRSTRLEN];
    printf("{\"data_packets\": %llu, \"context_packets\": %llu, \"malformed\": %llu, "
           "\"bytes\": %llu, \"packet_count_gaps\": %llu, \"other_peers\": %llu, "
           "\"spool_records\": %llu, \"spool_bytes\": %llu, "
           "\"spool_records_total\": %llu, \"spool_bytes_total\": %llu, "
           "\"rcvbuf\": %d, \"stop_reason\": \"%s\", \"peers\": [",
           data_packets, context_packets, malformed, bytes, count_gaps, other_peers,
           spool_records, spool_bytes, spool_records_total, spool_bytes_total, rcvbuf, stop_reason);
    for (unsigned i = 0; i < peer_count; i++) {
        inet_ntop(AF_INET, &peers[i].addr.sin_addr, ip, sizeof ip);
        printf("%s[\"%s\", %u, %llu]", i ? ", " : "", ip, ntohs(peers[i].addr.sin_port), peers[i].packets);
    }
    printf("]}\n");
}

int main(int argc, char **argv)
{
    if (argc < 4) {
        fprintf(stderr, "usage: udp_spool IPv4 PORT SPOOL_PATH [--max-bytes N] [--idle-ms N]\n");
        return 2;
    }
    char *end;
    long port = strtol(argv[2], &end, 10);
    if (*end || port < 1 || port > 65535)
        return 2;
    unsigned long long max_bytes = 0;
    long idle_ms = 200;
    for (int i = 4; i < argc; i++) {
        if (!strcmp(argv[i], "--max-bytes") && i + 1 < argc)
            max_bytes = strtoull(argv[++i], NULL, 0);
        else if (!strcmp(argv[i], "--idle-ms") && i + 1 < argc)
            idle_ms = strtol(argv[++i], NULL, 0);
        else
            return 2;
    }
    struct sockaddr_in bind_addr = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port)};
    if (inet_pton(AF_INET, argv[1], &bind_addr.sin_addr) != 1)
        return 2;

    struct sigaction term = {.sa_handler = on_term}, usr = {.sa_handler = on_reset};
    sigaction(SIGTERM, &term, NULL);
    sigaction(SIGINT, &term, NULL);
    sigaction(SIGUSR1, &usr, NULL);
    setvbuf(stderr, NULL, _IOLBF, 0);

    FILE *spool = fopen(argv[3], "wb");
    if (!spool) {
        perror("spool");
        return 1;
    }
    static char spool_buf[8 << 20];
    setvbuf(spool, spool_buf, _IOFBF, sizeof spool_buf);

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    int rcvbuf = 64 << 20;
    socklen_t optlen = sizeof rcvbuf;
    if (fd < 0 || setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof rcvbuf) ||
        getsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, &optlen)) {
        perror("UDP socket");
        return 1;
    }
    struct timeval timeout = {.tv_usec = 50000};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout);
    if (bind(fd, (struct sockaddr *)&bind_addr, sizeof bind_addr)) {
        perror("bind");
        return 1;
    }

    static unsigned char buffers[BATCH][DATAGRAM_MAX];
    struct sockaddr_in from[BATCH];
    struct iovec iov[BATCH];
    struct mmsghdr msgs[BATCH];
    fprintf(stderr, "ready rcvbuf=%d\n", rcvbuf);

    const char *stop_reason = "sigterm";
    double idle_since = 0;
    for (;;) {
        if (reset_requested) {
            reset_requested = 0;
            reset_accounting();
            fputs("reset\n", stderr);
        }
        for (int i = 0; i < BATCH; i++) {
            iov[i] = (struct iovec){buffers[i], DATAGRAM_MAX};
            msgs[i] = (struct mmsghdr){.msg_hdr = {.msg_name = &from[i], .msg_namelen = sizeof from[i],
                                                   .msg_iov = &iov[i], .msg_iovlen = 1}};
        }
        int n = recvmmsg(fd, msgs, BATCH, MSG_WAITFORONE, NULL);
        if (n > 0) {
            uint64_t mono = clock_ns(CLOCK_MONOTONIC), real = clock_ns(CLOCK_REALTIME);
            for (int i = 0; i < n; i++) {
                unsigned len = msgs[i].msg_len;
                classify(buffers[i], len, &from[i]);
                struct spool_hdr h = {
                    .mono_ns = mono, .real_ns = real, .len = len,
                    .peer_ip = ntohl(from[i].sin_addr.s_addr), .peer_port = ntohs(from[i].sin_port), .pad = 0,
                };
                if (fwrite(&h, sizeof h, 1, spool) != 1 || fwrite(buffers[i], 1, len, spool) != len) {
                    perror("spool write");
                    return 1;
                }
                spool_records++; spool_records_total++;
                spool_bytes += len; spool_bytes_total += len;
            }
            if (max_bytes && spool_bytes_total > max_bytes) {
                fflush(spool);
                stop_reason = "max-bytes";
                break;
            }
            idle_since = 0;
            continue;
        }
        if (n < 0 && errno != EAGAIN && errno != EINTR) {
            perror("recvmmsg");
            return 1;
        }
        if (!stop_requested)
            continue;
        if (!idle_since)
            idle_since = now();
        else if (now() - idle_since >= idle_ms / 1000.0)
            break;
    }
    if (fclose(spool)) {
        perror("spool close");
        return 1;
    }
    print_summary(rcvbuf, stop_reason);
    return strcmp(stop_reason, "max-bytes") ? 0 : 3;
}
