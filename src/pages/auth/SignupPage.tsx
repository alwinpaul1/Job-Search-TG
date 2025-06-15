import SignupForm from '../../components/auth/SignupForm'

export default function SignupPage() {
  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 via-blue-50 to-indigo-100 flex flex-col justify-center py-12 sm:px-6 lg:px-8 relative">
      {/* Background Elements */}
      <div className="fixed inset-0 overflow-hidden pointer-events-none">
        <div className="absolute -top-40 -right-40 w-80 h-80 bg-gradient-to-br from-purple-400 to-pink-400 rounded-full mix-blend-multiply filter blur-xl opacity-20 floating-animation"></div>
        <div className="absolute -bottom-40 -left-40 w-80 h-80 bg-gradient-to-br from-yellow-400 to-orange-400 rounded-full mix-blend-multiply filter blur-xl opacity-20 floating-animation" style={{ animationDelay: '2s' }}></div>
      </div>

      <div className="sm:mx-auto sm:w-full sm:max-w-md relative">
        <div className="text-center">
          <h1 className="text-4xl font-bold text-gradient">JobScout Pro</h1>
          <h2 className="mt-8 text-3xl font-bold text-gray-900">
            Create your account
          </h2>
          <p className="mt-3 text-lg text-gray-600">
            Start your journey to finding the perfect job
          </p>
        </div>
      </div>

      <div className="mt-8 sm:mx-auto sm:w-full sm:max-w-md relative">
        <div className="card shadow-2xl">
          <SignupForm />
        </div>
      </div>
    </div>
  )
}