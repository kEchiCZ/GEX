/** k-tý nejmenší prvek pole (quickselect, Hoare): jeden kvantil bez řazení.

Hledá se jedna hodnota, ne celé pořadí: O(n) místo O(n log n) a bez boxování
do JS pole. Plné řazení kvůli jednomu kvantilu bylo hlavním nákladem stavby
módu (#142: 256k buněk 108 ms → 2,5 ms) i prahů kontur (#1420). Výsledek je
tatáž hodnota jako `sorted[k]` po řazení — k-tý prvek multimnožiny nezávisí na
algoritmu.

Pole se přeuspořádá na místě, volající předává vlastní kopii. NaN v poli být
nesmí: porovnání v rozdělení by ho nezařadila. */
export function selectKth(values: Float32Array | Float64Array, k: number): number {
  let low = 0
  let high = values.length - 1
  while (low < high) {
    const pivot = values[(low + high) >> 1]
    let left = low
    let right = high
    while (left <= right) {
      while (values[left] < pivot) left += 1
      while (values[right] > pivot) right -= 1
      if (left <= right) {
        const swap = values[left]
        values[left] = values[right]
        values[right] = swap
        left += 1
        right -= 1
      }
    }
    if (k <= right) high = right
    else if (k >= left) low = left
    else break // cíl padl mezi oddíly — na své pozici už je hledaná hodnota
  }
  return values[k]
}
