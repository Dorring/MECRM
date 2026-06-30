'use client';

import { useSearchParams } from 'next/navigation';
import { KnowledgeBase } from '@/components/KnowledgeBase';

export default function KnowledgeArticlePageClient() {
  const params = useSearchParams();
  const id = params.get('id') || undefined;
  return <KnowledgeBase initialArticleId={id} />;
}
