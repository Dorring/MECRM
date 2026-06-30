'use client';

import { KnowledgeReview } from '@/components/KnowledgeReview';

export default function KnowledgeReviewPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Knowledge Review</h1>
        <p className="text-gray-500 dark:text-gray-400">Approve drafts to publish and embed articles.</p>
      </div>
      <KnowledgeReview />
    </div>
  );
}

