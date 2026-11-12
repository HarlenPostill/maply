import type { Status } from '../api';
import type { Dataset, Dimension, Filters, Selection, Unit } from './model';

export type View = 'overview' | 'map' | '3d' | 'compare' | 'time' | 'coverage' | 'streets' | 'photos';
export type Group = { title: string; subtitle: string; photos: number[] };

/** Everything a view needs. Selections are recomputed centrally so every view stays linked. */
export type Atlas = {
  ds: Dataset; dims: Dimension[]; dimMap: Map<string, Dimension>;
  sel: Selection; base: Selection; filters: Filters; unit: Unit;
  setFilters: (filters: Filters) => void;
  toggle: (dim: string, key: string, only?: boolean) => void;
  colorBy: Dimension; setColorBy: (id: string) => void;
  focus: string | null; setFocus: (key: string | null) => void;
  selectedPhoto: number | null; selectPhoto: (index: number | null) => void;
  group: Group | null; selectGroup: (group: Group | null) => void;
  status: Status; fitKey: string; goTo: (view: View) => void;
};
