/* Firebase web config for the hosted (iPad) version.
 *
 * Generated from: firebase apps:sdkconfig WEB (project millage-chordfinder).
 *
 * These values are NOT secrets - they identify the project, they don't grant
 * access. Access is controlled by Firestore rules + Google sign-in, so the
 * lyrics stay private to your account.
 *
 * The local PC version never uses these; it talks to serve.py.
 */
/* Anyone signed in can READ the library; only this account can import chord
 * sheets from a phone/tablet. Must match the owner list in
 * cloud/firestore.rules - the rules are what actually enforce it. */
window.CF_OWNER_EMAIL = "dylan.millage@gmail.com";

window.FIREBASE_CONFIG = {
  apiKey: "AIzaSyAW3uVN49_QWPjHuRb4xKpdfONkkhNsiQs",
  authDomain: "millage-chordfinder.firebaseapp.com",
  projectId: "millage-chordfinder",
  appId: "1:906955209442:web:84d2bbda8c922346d4fcbb",
  messagingSenderId: "906955209442",
};
