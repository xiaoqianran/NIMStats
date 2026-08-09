"""Versioned workloads and objective diagnostics for the rolling NIM benchmark."""

from __future__ import annotations

import re
from typing import Any

BENCHMARK_VERSION = "nimstats-v4-longgen-2026-08"

HEALTH_MARKER = "NIM_OK_7F3A"
HEALTH_PROMPT = (
    "Availability probe. Reply with exactly NIM_OK_7F3A and nothing else."
)

# Controlled 128-token output remains separate from the natural-stop long task.
THROUGHPUT_TARGET_TOKENS = 128
THROUGHPUT_MIN_VALID_TOKENS = 116  # ceil(128 * 0.90)
THROUGHPUT_PROMPT = """Performance workload. Write one continuous plain-English paragraph about a fictional library moving its catalog from paper cards to a digital archive. Do not use Markdown, lists, headings, code, quotations, or a conclusion. Begin exactly with "At dawn, the Atlas library" and keep adding concrete operational details until the platform stops generation. Do not stop early."""

LONG_TASK_EXPECTED_FILES = (
    "app/layout.tsx",
    "app/page.tsx",
    "app/blog/[slug]/page.tsx",
    "components/BlogExplorer.tsx",
    "lib/posts.ts",
    "app/globals.css",
)

LONG_TASK_PROMPT = """You are a senior frontend engineer. Build a complete, production-quality blog experience using Next.js App Router, React, TypeScript and plain CSS.

Return the complete source code for every requested file. Do not explain the implementation. Do not use Markdown prose outside the file blocks. Do not omit code, use placeholders, write ellipses, say "same as above", or shorten any file.

Output every file in this exact order and format:

<<<FILE:app/layout.tsx>>>
complete file content
<<<END_FILE>>>

<<<FILE:app/page.tsx>>>
complete file content
<<<END_FILE>>>

<<<FILE:app/blog/[slug]/page.tsx>>>
complete file content
<<<END_FILE>>>

<<<FILE:components/BlogExplorer.tsx>>>
complete file content
<<<END_FILE>>>

<<<FILE:lib/posts.ts>>>
complete file content
<<<END_FILE>>>

<<<FILE:app/globals.css>>>
complete file content
<<<END_FILE>>>

The application must include:

1. A responsive header with brand, navigation and mobile menu.
2. A visually prominent featured article.
3. At least eight realistic blog posts with complete metadata and article content.
4. Search across titles, excerpts, categories and authors.
5. Category filtering.
6. Sorting by newest, oldest, most viewed and reading time.
7. A responsive article card grid.
8. A useful empty-search state.
9. Individual dynamic article pages using the slug route.
10. generateStaticParams for every article.
11. Dynamic metadata for article pages.
12. Author, publication date, reading time, tags and view count.
13. Previous and next article navigation.
14. A related-articles section.
15. A newsletter subscription form with client-side validation.
16. Semantic HTML and accessible labels.
17. Visible keyboard focus styles.
18. Dark and light color-scheme support.
19. Reduced-motion support.
20. Responsive layouts for desktop, tablet and mobile.
21. CSS custom properties for colors, spacing, typography and radius.
22. No external component libraries, icon libraries or network requests.
23. No remote images; create visual covers using CSS gradients.
24. No dangerouslySetInnerHTML.
25. No TODO comments or incomplete implementations.

Write every file completely. Continue until all six files have been emitted and closed with <<<END_FILE>>>."""


def analyze_long_response(
    text: str | None,
    finish_reason: str | None,
) -> dict[str, Any]:
    """Describe observable output completeness without assigning a quality score."""
    response = text or ""
    file_paths = re.findall(r"(?m)^<<<FILE:([^>\r\n]+)>>>\s*$", response)
    closed_files = len(re.findall(r"(?m)^<<<END_FILE>>>\s*$", response))
    expected_present = sum(path in file_paths for path in LONG_TASK_EXPECTED_FILES)
    protocol_complete = (
        expected_present == len(LONG_TASK_EXPECTED_FILES)
        and closed_files >= len(LONG_TASK_EXPECTED_FILES)
        and response.rstrip().endswith("<<<END_FILE>>>")
    )
    return {
        "responseChars": len(response),
        "filesEmitted": len(file_paths),
        "filesComplete": closed_files,
        "expectedFilesPresent": expected_present,
        "outputComplete": protocol_complete,
        "truncated": finish_reason == "length",
    }
