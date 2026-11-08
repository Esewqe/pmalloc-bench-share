#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <malloc.h>
#include <thread>
#include <unistd.h>
#include <vector>

static void check(bool value, const char* message) {
    if (!value) {
        std::fprintf(stderr, "FAIL: %s\n", message);
        std::abort();
    }
}

int main() {
    Dl_info info{};
    check(dladdr(reinterpret_cast<void*>(&malloc), &info) != 0,
          "malloc symbol provenance");
    std::printf("malloc provider: %s\n", info.dli_fname);
    std::fflush(stdout);

    for (std::size_t n : {std::size_t(1), std::size_t(16), std::size_t(64),
                          std::size_t(8192), std::size_t(8193), std::size_t(1048576)}) {
        auto* p = static_cast<unsigned char*>(std::malloc(n));
        check(p != nullptr, "malloc");
        check(malloc_usable_size(p) >= n, "usable size");
        std::memset(p, 0xA5, n);

        auto* q = static_cast<unsigned char*>(std::realloc(p, n + 100));
        check(q != nullptr, "realloc");
        for (std::size_t i = 0; i < n; ++i) {
            check(q[i] == 0xA5, "realloc preserved prefix");
        }
        std::free(q);

        p = static_cast<unsigned char*>(std::calloc(n, 1));
        check(p != nullptr, "calloc");
        for (std::size_t i = 0; i < n; ++i) {
            check(p[i] == 0, "calloc zeroed");
        }
        std::free(p);
    }

    volatile std::size_t huge = SIZE_MAX;
    errno = 0;
    void* overflow = std::calloc(huge, 2);
    const int overflow_errno = errno;
    std::printf("calloc_overflow: returned_null=%d errno=%d ENOMEM=%d\n",
                overflow == nullptr, overflow_errno, ENOMEM);
    check(overflow == nullptr, "calloc overflow returns null");

    void* untouched = reinterpret_cast<void*>(std::uintptr_t(0x1234));
    check(posix_memalign(&untouched, 3, 100) == EINVAL &&
              untouched == reinterpret_cast<void*>(std::uintptr_t(0x1234)),
          "invalid posix alignment leaves output unchanged");

    void* aligned = nullptr;
    check(posix_memalign(&aligned, 32768, 70000) == 0, "valid posix alignment");
    check(reinterpret_cast<std::uintptr_t>(aligned) % 32768 == 0, "aligned pointer");
    std::memset(aligned, 0x5A, 70000);
    std::free(aligned);

    char* s = strdup("allocator-smoke");
    check(s && std::strcmp(s, "allocator-smoke") == 0, "strdup");
    std::free(s);

    s = strndup("allocator-smoke", 9);
    check(s && std::strcmp(s, "allocator") == 0, "strndup");
    std::free(s);

    void* odd = aligned_alloc(64, 100);
    check(odd && reinterpret_cast<std::uintptr_t>(odd) % 64 == 0,
          "aligned_alloc with size not a multiple of alignment");
    std::memset(odd, 0x3C, 100);
    std::free(odd);

    void* page_aligned = memalign(4096, 100);
    check(page_aligned && reinterpret_cast<std::uintptr_t>(page_aligned) % 4096 == 0,
          "memalign");
    std::free(page_aligned);

    const auto page = static_cast<std::uintptr_t>(sysconf(_SC_PAGESIZE));
    void* paged = valloc(100);
    check(paged && reinterpret_cast<std::uintptr_t>(paged) % page == 0, "valloc");
    std::free(paged);

    check(reallocarray(nullptr, huge, 2) == nullptr, "reallocarray overflow returns null");
    auto* array = static_cast<unsigned char*>(reallocarray(nullptr, 10, 10));
    check(array != nullptr, "reallocarray");
    std::memset(array, 0x7E, 100);
    array = static_cast<unsigned char*>(reallocarray(array, 100, 10));
    check(array && array[0] == 0x7E && array[99] == 0x7E, "reallocarray preserved prefix");
    std::free(array);

    constexpr std::size_t count = 16384, threads = 8;
    std::vector<void*> blocks(count);
    for (std::size_t i = 0; i < count; ++i) {
        blocks[i] = std::malloc(64);
        check(blocks[i] != nullptr, "remote allocation");
        std::memset(blocks[i], static_cast<int>(i & 255), 64);
    }

    std::atomic<bool> go{false};
    std::vector<std::thread> workers;
    for (std::size_t t = 0; t < threads; ++t) {
        workers.emplace_back([&, t] {
            while (!go.load(std::memory_order_acquire)) {
                std::this_thread::yield();
            }
            for (std::size_t i = t; i < count; i += threads) {
                auto* p = static_cast<unsigned char*>(blocks[i]);
                check(p[0] == (i & 255) && p[63] == (i & 255), "remote payload");
                std::free(p);
            }
        });
    }

    go.store(true, std::memory_order_release);
    for (std::size_t i = 0; i < count; ++i) {
        void* p = std::malloc(64);
        check(p != nullptr, "concurrent owner allocation");
        std::free(p);
    }
    for (auto& worker : workers) {
        worker.join();
    }

    std::puts("PASS: calloc/realloc payload, usable size, overflow, alignment, "
              "aligned_alloc/memalign/valloc, reallocarray, string allocation, "
              "concurrent cross-thread frees");
}
