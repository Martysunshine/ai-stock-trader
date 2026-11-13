import { Marked } from 'marked';

/** Replies are local files, but raw HTML in them is shown as text, and only web links are links. */
const escape = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const md = new Marked({
  gfm: true,
  breaks: true,
  renderer: {
    html: ({ text }: { text: string }) => escape(text),
    link({ href, text }: { href: string; text: string }) {
      if (!/^https?:\/\//i.test(href)) return escape(text);
      return `<a href="${escape(href)}" target="_blank" rel="noreferrer noopener">${escape(text)}</a>`;
    },
    image: ({ text }: { text: string }) => escape(text),
  },
});

export const renderMarkdown = (text: string) => md.parse(text, { async: false }) as string;
