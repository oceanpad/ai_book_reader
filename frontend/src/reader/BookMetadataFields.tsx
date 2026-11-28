import type { BookLanguage, BookLevel } from "../api/contracts.generated";

const levels: BookLevel[] = ["P1", "P2", "P3", "P4", "P5", "P6"];
export const bookLanguages = { en: "English", zh: "Chinese" } satisfies Record<BookLanguage, string>;
export const languageOptions: BookLanguage[] = ["en", "zh"];
export type BookMetadata = { level: BookLevel | null; language: BookLanguage | null };

type Props = { prefix: string; value: BookMetadata; disabled: boolean; onChange: (value: BookMetadata) => void };

export function BookMetadataFields({ prefix, value, disabled, onChange }: Props) {
  return <div className="book-metadata-fields">
    <label htmlFor={`${prefix}-level`}>Level<select id={`${prefix}-level`} value={value.level ?? ""} disabled={disabled} onChange={event => onChange({ ...value, level: levels.find(level => level === event.target.value) ?? null })}>
      <option value="">Not set</option>{levels.map(level => <option key={level} value={level}>{level}</option>)}
    </select></label>
    <label htmlFor={`${prefix}-language`}>Language<select id={`${prefix}-language`} value={value.language ?? ""} disabled={disabled} onChange={event => onChange({ ...value, language: languageOptions.find(language => language === event.target.value) ?? null })}>
      <option value="">Not set</option>{languageOptions.map(language => <option key={language} value={language}>{bookLanguages[language]}</option>)}
    </select></label>
  </div>;
}
