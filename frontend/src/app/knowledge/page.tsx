'use client';

import Link from 'next/link';
import { KnowledgeBase } from '@/components/KnowledgeBase';

export default function KnowledgeHomePage() {
  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Knowledge</h1>
          <p className="text-gray-500 dark:text-gray-400">Self-growing documentation curated by humans.</p>
        </div>
        <Link href="/knowledge/review" className="btn btn-secondary">
          Review Drafts
        </Link>
      </div>
      <KnowledgeBase />
    </div>
  );
}

