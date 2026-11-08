#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <malloc.h>
#include <unistd.h>

#include "pmalloc_posix.hpp"

#define EXPORT __attribute__((visibility("default")))

static bool power_of_two(std::size_t n) noexcept {
    return n != 0 && (n & (n - 1)) == 0;
}

static void* allocated(void* p) noexcept {
    if (!p) {
        errno = ENOMEM;
    }
    return p;
}

extern "C" {

EXPORT void* malloc(std::size_t n) noexcept {
    return allocated(pmalloc::pm_malloc(n));
}

EXPORT void free(void* p) noexcept {
    const int saved = errno;
    pmalloc::pm_free(p);
    errno = saved;
}

EXPORT void* calloc(std::size_t n, std::size_t size) noexcept {
    return allocated(pmalloc::pm_calloc(n, size));
}

EXPORT void* realloc(void* p, std::size_t n) noexcept {
    if (p && n == 0) {
        free(p);
        return nullptr;
    }
    return allocated(pmalloc::pm_realloc(p, n));
}

EXPORT void* reallocarray(void* p, std::size_t n, std::size_t size) noexcept {
    if (size != 0 && n > SIZE_MAX / size) {
        errno = ENOMEM;
        return nullptr;
    }
    return realloc(p, n * size);
}

EXPORT std::size_t malloc_usable_size(void* p) noexcept {
    return pmalloc::pm_usable_size(p);
}

EXPORT int posix_memalign(void** out, std::size_t alignment, std::size_t n) noexcept {
    if (alignment < sizeof(void*) || !power_of_two(alignment)) {
        return EINVAL;
    }
    const int saved = errno;
    void* p = pmalloc::pm_malloc_aligned(n, alignment);
    errno = saved;
    if (!p) {
        return ENOMEM;
    }
    *out = p;
    return 0;
}

// Like glibc and mimalloc, accept sizes that are not a multiple of the
// alignment (C17 DR 460 dropped that requirement).
EXPORT void* aligned_alloc(std::size_t alignment, std::size_t n) noexcept {
    if (!power_of_two(alignment)) {
        errno = EINVAL;
        return nullptr;
    }
    return allocated(pmalloc::pm_malloc_aligned(n, alignment));
}

EXPORT void* memalign(std::size_t alignment, std::size_t n) noexcept {
    if (!power_of_two(alignment)) {
        errno = EINVAL;
        return nullptr;
    }
    return allocated(pmalloc::pm_malloc_aligned(n, alignment));
}

EXPORT void* valloc(std::size_t n) noexcept {
    return allocated(pmalloc::pm_malloc_aligned(n, pmalloc::page_size()));
}

EXPORT void* pvalloc(std::size_t n) noexcept {
    const std::size_t ps = pmalloc::page_size();
    if (n > SIZE_MAX - (ps - 1)) {
        errno = ENOMEM;
        return nullptr;
    }
    return allocated(pmalloc::pm_malloc_aligned(pmalloc::align_up(n, ps), ps));
}

EXPORT void cfree(void* p) noexcept {
    free(p);
}

EXPORT char* strdup(const char* s) noexcept {
    const std::size_t n = std::strlen(s);
    auto* p = static_cast<char*>(malloc(n + 1));
    if (p) {
        std::memcpy(p, s, n + 1);
    }
    return p;
}

EXPORT char* strndup(const char* s, std::size_t limit) noexcept {
    const std::size_t n = ::strnlen(s, limit);
    auto* p = static_cast<char*>(malloc(n + 1));
    if (p) {
        std::memcpy(p, s, n);
        p[n] = '\0';
    }
    return p;
}

}
