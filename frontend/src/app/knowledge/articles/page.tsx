import { Suspense } from 'react';
import KnowledgeArticlePageClient from './page-client';

export default function KnowledgeArticlePage() {
  return (
    <Suspense>
      <KnowledgeArticlePageClient />
    </Suspense>
  );
}

